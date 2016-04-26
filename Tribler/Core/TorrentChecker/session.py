from abc import ABCMeta, abstractmethod, abstractproperty
import logging
import random
import struct
import time

from libtorrent import bdecode
from twisted.internet import reactor, defer
from twisted.internet.defer import Deferred, maybeDeferred, DeferredList
from twisted.internet.protocol import DatagramProtocol
from twisted.web.client import Agent, readBody, RedirectAgent

from Tribler.Core.Utilities.encoding import add_url_params
from Tribler.Core.Utilities.tracker_utils import parse_tracker_url
from Tribler.dispersy.util import call_on_reactor_thread

# Although these are the actions for UDP trackers, they can still be used as
# identifiers.
TRACKER_ACTION_CONNECT = 0
TRACKER_ACTION_ANNOUNCE = 1
TRACKER_ACTION_SCRAPE = 2

MAX_INT32 = 2 ** 16 - 1

UDP_TRACKER_INIT_CONNECTION_ID = 0x41727101980
UDP_TRACKER_RECHECK_INTERVAL = 15
UDP_TRACKER_MAX_RETRIES = 8

HTTP_TRACKER_RECHECK_INTERVAL = 60
HTTP_TRACKER_MAX_RETRIES = 0

DHT_TRACKER_RECHECK_INTERVAL = 60
DHT_TRACKER_MAX_RETRIES = 8

MAX_TRACKER_MULTI_SCRAPE = 74


def create_tracker_session(tracker_url, on_result_callback):
    """
    Creates a tracker session with the given tracker URL.
    :param tracker_url: The given tracker URL.
    :param on_result_callback: The on_result callback.
    :return: The tracker session.
    """
    tracker_type, tracker_address, announce_page = parse_tracker_url(tracker_url)

    if tracker_type == u'UDP':
        return UdpTrackerSession(tracker_url, tracker_address, announce_page, on_result_callback)
    else:
        return HttpTrackerSession(tracker_url, tracker_address, announce_page, on_result_callback)


class TrackerSession(object):
    __meta__ = ABCMeta

    def __init__(self, tracker_type, tracker_url, tracker_address, announce_page, on_result_callback):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tracker_type = tracker_type
        self._tracker_url = tracker_url
        self._tracker_address = tracker_address
        self._announce_page = announce_page

        self._infohash_list = []
        self.result_deferred = None

        self._on_result_callback = on_result_callback

        self._retries = 0

        self._last_contact = None
        self._action = None

        # some flags
        self._is_initiated = False  # you cannot add requests to a session if it has been initiated
        self._is_finished = False
        self._is_failed = False
        self._is_timed_out = False

    def __str__(self):
        return "Tracker[%s, %s]" % (self._tracker_type, self._tracker_url)

    def __unicode__(self):
        return u"Tracker[%s, %s]" % (self._tracker_type, self._tracker_url)

    def cleanup(self):
        """
        Sets the _infohash_list to None and returns a deferred that has succeeded.
        :return: A deferred that succeeds immediately.
        """
        self._infohash_list = None
        return defer.succeed(None)

    def can_add_request(self):
        """
        Checks if we still can add requests to this session.
        :return: True or False.
        """
        return not self._is_initiated and len(self._infohash_list) < MAX_TRACKER_MULTI_SCRAPE

    def has_request(self, infohash):
        return infohash in self._infohash_list

    def add_request(self, infohash):
        """
        Adds a request into this session.
        :param infohash: The infohash to be added.
        """
        assert not self._is_initiated, u"Must not add request to an initiated session."
        assert not self.has_request(infohash), u"Must not add duplicate requests"
        self._infohash_list.append(infohash)

    @abstractmethod
    def create_connection(self):
        """Creates a connection to the tracker."""
        self._is_timed_out = False

    @abstractmethod
    def connect_to_tracker(self):
        """Does some work when a connection has been established."""
        pass

    @abstractproperty
    def max_retries(self):
        """Number of retries before a session is marked as failed."""
        pass

    @abstractproperty
    def retry_interval(self):
        """Interval between retries."""
        pass

    @property
    def tracker_type(self):
        return self._tracker_type

    @property
    def tracker_url(self):
        return self._tracker_url

    @property
    def infohash_list(self):
        return self._infohash_list

    @property
    def last_contact(self):
        return self._last_contact

    @property
    def action(self):
        return self._action

    @property
    def retries(self):
        return self._retries

    def increase_retries(self):
        self._retries += 1

    @property
    def is_initiated(self):
        return self._is_initiated

    @property
    def is_finished(self):
        return self._is_finished

    @property
    def is_failed(self):
        return self._is_failed

    @property
    def is_timed_out(self):
        return self._is_timed_out


class HttpTrackerSession(TrackerSession):
    def __init__(self, tracker_url, tracker_address, announce_page, on_result_callback):
        super(HttpTrackerSession, self).__init__(u'HTTP', tracker_url, tracker_address, announce_page,
                                                 on_result_callback)
        self._header_buffer = None
        self._message_buffer = None
        self._content_encoding = None
        self._content_length = None
        self._received_length = None
        self.result_deferred = None
        self.request = None

    def max_retries(self):
        """
        Returns the max amount of retries allowed for this session.
        :return: The maximum amount of retries.
        """
        return HTTP_TRACKER_MAX_RETRIES

    def retry_interval(self):
        """
        Returns the interval one has to wait before retrying to connect.
        :return: The interval before retrying.
        """
        return HTTP_TRACKER_RECHECK_INTERVAL

    def create_connection(self):
        super(HttpTrackerSession, self).create_connection()
        self._action = TRACKER_ACTION_CONNECT
        return True

    def connect_to_tracker(self):
        # create the HTTP GET message
        # Note: some trackers have strange URLs, e.g.,
        #       http://moviezone.ws/announce.php?passkey=8ae51c4b47d3e7d0774a720fa511cc2a
        #       which has some sort of 'key' as parameter, so we need to use the add_url_params
        #       utility function to handle such cases.

        url = add_url_params("http://%s:%s/%s" %
                             (self._tracker_address[0], self._tracker_address[1],
                              self._announce_page.replace(u'announce', u'scrape')),
                             {"info_hash": self._infohash_list})

        agent = RedirectAgent(Agent(reactor, connectTimeout=15.0))
        self.request = agent.request('GET', bytes(url))
        self.request.addCallback(self.on_response)
        self.request.addErrback(self.on_error)

        self._logger.debug(u"%s HTTP SCRAPE message sent: %s", self, url)

        # no more requests can be appended to this session
        self._action = TRACKER_ACTION_SCRAPE
        self._is_initiated = True
        self._last_contact = int(time.time())

        # Return deferred that will evaluate when the whole chain is done.
        self.result_deferred = Deferred(self._on_cancel)
        return self.result_deferred

    def on_error(self, failure):
        """
        Handles the case of an error during the request.
        :param failure: The failure object that is thrown by a deferred.
        """
        self._logger.info("Error when querying http tracker: %s %s", str(failure), self.tracker_url)
        self.failed()

    def on_response(self, response):
        # Check if this one was OK.
        if response.code != 200:
            # error response code
            self._logger.warning(u"%s HTTP SCRAPE error response code [%s, %s]", self, response.code, response.phrase)
            self.failed()
            return

        # All ok, parse the body
        d = readBody(response)
        d.addCallbacks(self._process_scrape_response, self.on_error)

    def _on_cancel(self, a):
        """
        :param _: The deferred which we ignore.
        This function handles the scenario of the session prematurely being cleaned up,
        most likely due to a shutdown.
        This function only should be called by the result_deferred.
        """
        self._logger.info(
            "The result deferred of this HTTP tracker session is being cancelled due to a session cleanup. HTTP url: %s",
            self.tracker_url)

    def failed(self):
        """
        This method handles everything that needs to be done when one step
        in the session has failed and thus no data can be obtained.
        """
        self._is_failed = True
        if self.result_deferred:
            self.result_deferred.errback(ValueError("HTTP tracker has failed for url %s" % self._tracker_url))

    def _process_scrape_response(self, body):
        """
        This function handles the response body of a HTTP tracker,
        parsing the results.
        """
        # parse the retrieved results
        if body is None:
            self.failed()
            return

        response_dict = bdecode(body)
        if response_dict is None:
            self.failed()
            return

        seed_leech_dict = {}

        unprocessed_infohash_list = self._infohash_list[:]
        if 'files' in response_dict and isinstance(response_dict['files'], dict):
            for infohash in response_dict['files']:
                complete = response_dict['files'][infohash].get('complete', 0)
                incomplete = response_dict['files'][infohash].get('incomplete', 0)

                # Sow complete as seeders. "complete: number of peers with the entire file, i.e. seeders (integer)"
                #  - https://wiki.theory.org/BitTorrentSpecification#Tracker_.27scrape.27_Convention
                seeders = complete
                leechers = incomplete

                # Store the information in the dictionary
                seed_leech_dict[infohash] = (seeders, leechers)

                # remove this infohash in the infohash list of this session
                if infohash in unprocessed_infohash_list:
                    unprocessed_infohash_list.remove(infohash)

        elif 'failure reason' in response_dict:
            self._logger.info(u"%s Failure as reported by tracker [%s]", self, repr(response_dict['failure reason']))
            self.failed()
            return

        # handle the infohashes with no result (seeders/leechers = 0/0)
        for infohash in unprocessed_infohash_list:
            seeders, leechers = 0, 0
            seed_leech_dict[infohash] = (seeders, leechers)

        self._is_finished = True
        self.result_deferred.callback(seed_leech_dict)

    def cleanup(self):
        """
        Cleans the session by cancelling all deferreds and closing sockets.
        :return: A deferred that fires once the cleanup is done.
        """
        super(HttpTrackerSession, self).cleanup()
        if self.request:
            self.request.cancel()

        if self.result_deferred:
            self.result_deferred.cancel()

        self.result_deferred = None
        return defer.succeed(None)


class UDPScraper(DatagramProtocol):
    """
    The UDP scraper connects to a UDP tracker and queries
    seeders and leechers for every infohash appended to the UDPsession.
    All data received is given to the UDP session it's associated with.
    """

    _reactor = reactor

    def __init__(self, udpsession, ip_address, port):
        self._logger = logging.getLogger(self.__class__.__name__)
        self.udpsession = udpsession
        self.ip_address = ip_address
        self.port = port
        self.expect_connection_response = True
        # Timeout after 15 seconds if nothing received.
        self.timeout_seconds = 15
        self.timeout = self._reactor.callLater(self.timeout_seconds, self.on_error)

    def on_error(self):
        """
        This method handles everything that needs to be done when something during
        the UDP scraping went wrong.
        """
        self.udpsession.failed()

    def stop(self):
        """
        Stops the UDP scraper and closes the socket.
        :return: A deferred that fires once it has closed the connection.
        """
        self._logger.info("Shutting down scraper which was connected to ip %s, port %s", self.ip_address, self.port)
        if self.timeout.active():
            self.timeout.cancel()

        if self.transport and self.numPorts and self.transport.connected:
            return maybeDeferred(self.transport.stopListening)
        return defer.succeed(True)

    def startProtocol(self):
        """
        This function is called when the scraper is initialized.
        Initiates the connection with the tracker.
        """
        self.transport.connect(self.ip_address, self.port)
        self._logger.info("UDP health scraper connected to host %s port %d", self.ip_address, self.port)
        self.udpsession.on_start()

    def write_data(self, data):
        """
        This function can be called to send serialized data to the tracker.
        :param data: The serialized data to be send.
        """
        self.transport.write(data)  # no need to pass the ip and port

    def datagramReceived(self, data, (_host, _port)):
        """
        This function dispatches data received from a UDP tracker.
        If it's the first response, it will dispatch the data to the handle_connection_response
        function of the UDP session.
        All subsequent data will be send to the _handle_response function of the UDP session.
        :param data: The data received from the UDP tracker.
        """
        # If we expect a connection response, pass it to handle connection response
        if self.expect_connection_response:
            # Cancel the timeout
            if self.timeout.active():
                self.timeout.cancel()

            # Pass the response to the udp tracker session
            self.udpsession.handle_connection_response(data)
            self.expect_connection_response = False
        # else it is our scraper payload. Give it to handle response
        else:
            self.udpsession.handle_response(data)

    # Possibly invoked if there is no server listening on the
    # address to which we are sending.
    def connectionRefused(self):
        """
        Handles the case of a connection being refused by a tracker.
        """
        self._logger.info("UDP Scraper could not connect to %s %s", self.ip_address, self.port)
        self.on_error()


class UdpTrackerSession(TrackerSession):
    """
    The UDPTrackerSession makes a connection with a UDP tracker by making use
    of a UDPScraper object. It handles the message serialization and communication
    with the torrenchecker by making use of Deferred (asynchronously).
    """

    # A list of transaction IDs that have been used in order to avoid conflict.
    _active_session_dict = dict()

    def __init__(self, tracker_url, tracker_address, announce_page, on_result_callback):
        super(UdpTrackerSession, self).__init__(u'UDP', tracker_url, tracker_address, announce_page, on_result_callback)
        self._connection_id = 0
        self._transaction_id = 0
        self.transaction_id = 0
        self.port = tracker_address[1]
        self.ip_address = None
        self.scraper = None
        self.ip_resolve_deferred = None
        self.clean_defer_list = []

    def on_error(self, failure):
        """
        Handles the case when resolving an ip address fails.
        :param failure: The failure object thrown by the deferred.
        """
        self._logger.info("Error when querying UDP tracker: %s %s", str(failure), self.tracker_url)
        self.failed()

    def _on_cancel(self, _):
        """
        :param _: The deferred which we ignore.
        This function handles the scenario of the session prematurely being cleaned up,
        most likely due to a shutdown.
        This function only should be called by the result_deferred.
        """
        self._logger.info(
            "The result deferred of this UDP tracker session is being cancelled due to a session cleanup. UDP url: %s",
            self.tracker_url)

    def on_ip_address_resolved(self, ip_address):
        """
        Called when a hostname has been resolved to an ip address.
        Constructs a scraper and opens a UDP port to listen on.
        Removes an old scraper if present.
        :param ip_address: The ip address that matches the hostname of the tracker_url.
        """
        self.ip_address = ip_address
        # Close the old scraper if present.
        if self.scraper:
            self.clean_defer_list.append(self.scraper.stop())
        self.scraper = UDPScraper(self, self.ip_address, self.port)
        reactor.listenUDP(0, self.scraper)

    def failed(self):
        """
        This method handles everything that needs to be done when one step
        in the session has failed and thus no data can be obtained.
        """
        self._is_failed = True
        if self.result_deferred:
            self.result_deferred.errback(ValueError("UDP tracker failed for url %s" % self._tracker_url))

    def generate_transaction_id(self):
        """
        Generates a unique transaction id and stores this in the _active_session_dict set.
        """
        while True:
            # make sure there is no duplicated transaction IDs
            transaction_id = random.randint(0, MAX_INT32)
            if transaction_id not in UdpTrackerSession._active_session_dict.items():
                UdpTrackerSession._active_session_dict[self] = transaction_id
                self.transaction_id = transaction_id
                break

    @staticmethod
    def remove_transaction_id(session):
        """
        Removes an session and its corresponding id from the _active_session_dict set.
        :param session: The session that needs to be removed from the set.
        """
        if session in UdpTrackerSession._active_session_dict:
            del UdpTrackerSession._active_session_dict[session]

    def cleanup(self):
        """
        Cleans the session by cancelling all deferreds and closing sockets.
        :return: A deferred that fires once the cleanup is done.
        """
        super(UdpTrackerSession, self).cleanup()
        UdpTrackerSession.remove_transaction_id(self)
        # Cleanup deferred that fires when everything has been cleaned
        # Cancel the resolving ip deferred.
        if self.ip_resolve_deferred:
            self.ip_resolve_deferred.cancel()

        if self.result_deferred:
            self.result_deferred.cancel()

        if self.scraper:
            self.clean_defer_list.append(self.scraper.stop())
            del self.scraper

        # Return a deferredlist with all clean deferreds we have to wait on
        return DeferredList(self.clean_defer_list)

    def max_retries(self):
        """
        Returns the max amount of retries allowed for this session.
        :return: The maximum amount of retries.
        """
        return UDP_TRACKER_MAX_RETRIES

    def retry_interval(self):
        """
        Returns the time one has to wait until retrying the connection again.
        Increases exponentially with the number of retries.
        :return: The interval one has to wait before retrying the connection.
        """
        return UDP_TRACKER_RECHECK_INTERVAL * (2 ** self._retries)

    def create_connection(self):
        """
        Sets the connection_id, _action and transaction_id.
        :return: True if all is successful.
        """
        super(UdpTrackerSession, self).create_connection()
        # prepare connection message
        self._connection_id = UDP_TRACKER_INIT_CONNECTION_ID
        self._action = TRACKER_ACTION_CONNECT
        self.generate_transaction_id()

        return True

    def connect_to_tracker(self):
        """
        Connects to the tracker and starts querying for seed and leech data.
        :return: A deferred that will fire with a dictionary containg seed/leech information per infohash
        """
        # no more requests can be appended to this session
        self._is_initiated = True

        # clean old deferreds if present
        if self.result_deferred:
            self.result_deferred.cancel()
        if self.ip_resolve_deferred:
            self.ip_resolve_deferred.cancel()

        # Resolve the hostname to an IP address if not done already
        self.ip_resolve_deferred = reactor.resolve(self._tracker_address[0])
        self.ip_resolve_deferred.addCallbacks(self.on_ip_address_resolved, self.on_error)

        self._last_contact = int(time.time())

        self.result_deferred = Deferred(self._on_cancel)
        return self.result_deferred

    def on_start(self):
        """
        Called by the UDPScraper when it is connected to the tracker.
        Creates a connection message and calls the scraper to send it.
        """
        # Initiate the connection
        message = struct.pack('!qii', self._connection_id, self._action, self._transaction_id)
        self.scraper.write_data(message)

    def handle_connection_response(self, response):
        """
        Handles the connection response from the UDP scraper and queries
        it immediately for seed/leech data per infohash
        :param response: The connection response from the UDP scraper
        """
        if self.is_failed:
            return

        # check message size
        if len(response) < 16:
            self._logger.error(u"%s Invalid response for UDP CONNECT: %s", self, repr(response))
            self.failed()
            return

        # check the response
        action, transaction_id = struct.unpack_from('!ii', response, 0)
        if action != self._action or transaction_id != self._transaction_id:
            # get error message
            errmsg_length = len(response) - 8
            error_message = struct.unpack_from('!' + str(errmsg_length) + 's', response, 8)

            self._logger.info(u"%s Error response for UDP CONNECT [%s]: %s",
                              self, repr(response), repr(error_message))
            self.failed()
            return

        # update action and IDs
        self._connection_id = struct.unpack_from('!q', response, 8)[0]
        self._action = TRACKER_ACTION_SCRAPE
        self.generate_transaction_id()

        # pack and send the message
        fmt = '!qii' + ('20s' * len(self._infohash_list))
        message = struct.pack(fmt, self._connection_id, self._action, self._transaction_id, *self._infohash_list)

        # Send the scrape message
        self.scraper.write_data(message)

        self._last_contact = int(time.time())

    def handle_response(self, response):
        """
        Handles the response from the UDP scraper.
        :param response: The response from the UDP scraper
        """
        if self._is_failed:
            return

        # check message size
        if len(response) < 8:
            self._logger.info(u"%s Invalid response for UDP SCRAPE: %s", self, repr(response))
            self.failed()
            return

        # check response
        action, transaction_id = struct.unpack_from('!ii', response, 0)
        if action != self._action or transaction_id != self._transaction_id:
            # get error message
            errmsg_length = len(response) - 8
            error_message = \
                struct.unpack_from('!' + str(errmsg_length) + 's', response, 8)

            self._logger.info(u"%s Error response for UDP SCRAPE: [%s] [%s]",
                              self, repr(response), repr(error_message))
            self.failed()
            return

        # get results
        if len(response) - 8 != len(self._infohash_list) * 12:
            self._logger.info(u"%s UDP SCRAPE response mismatch: %s", self, len(response))
            self.failed()
            return

        offset = 8

        seed_leech_dict = {}

        for infohash in self._infohash_list:
            complete, _downloaded, incomplete = struct.unpack_from('!iii', response, offset)
            offset += 12

            # Store the information in the hash dict to be returned.
            # Sow complete as seeders. "complete: number of peers with the entire file, i.e. seeders (integer)"
            #  - https://wiki.theory.org/BitTorrentSpecification#Tracker_.27scrape.27_Convention
            seed_leech_dict[infohash] = (complete, incomplete)

        # close this socket and remove its transaction ID from the list
        UdpTrackerSession.remove_transaction_id(self)
        self._is_finished = True

        # Call the  callback of the deferred with the result
        self.scraper.stop()
        self.result_deferred.callback(seed_leech_dict)


class FakeDHTSession(TrackerSession):
    """
    Fake TrackerSession that manages DHT requests
    """

    def __init__(self, session, on_result_callback):
        super(FakeDHTSession, self).__init__(u'DHT', u'DHT', u'DHT', u'DHT', on_result_callback)

        self.result_deferred = None
        self._session = session

    def cleanup(self):
        """
        Cleans the session by cancelling all deferreds and closing sockets.
        :return: A deferred that fires once the cleanup is done.
        """
        self._infohash_list = None
        self._session = None
        # Return a defer that immediately calls its callback
        return defer.succeed(None)

    def can_add_request(self):
        """
        Returns whether or not this session can accept additional infohashes.
        :return:
        """
        return True

    def add_request(self, infohash):
        """
        This function adds a infohash to the request list.
        :param infohash: The infohash to be added.
        """

        @call_on_reactor_thread
        def on_metainfo_received(metainfo):
            seed_leech_dict = {}
            seed_leech_dict[infohash] = (metainfo['seeders'], metainfo['leechers'])
            self._on_result_callback(seed_leech_dict)

        @call_on_reactor_thread
        def on_metainfo_timeout(result_info_hash):
            seeder_leecher_dict = {}
            seeder_leecher_dict[result_info_hash] = (0, 0)
            self._on_result_callback(seeder_leecher_dict)

        if self._session:
            self._session.lm.ltmgr.get_metainfo(infohash, callback=on_metainfo_received,
                                                timeout_callback=on_metainfo_timeout)

    def create_connection(self):
        pass

    def connect_to_tracker(self):
        """
        Fakely connects to a tracker.
        :return: A deferred with a callback containing an empty dictionary.
        """
        return defer.succeed(dict())

    def _handle_response(self):
        pass

    @property
    def max_retries(self):
        """
        Returns the max amount of retries allowed for this session.
        :return: The maximum amount of retries.
        """
        return DHT_TRACKER_MAX_RETRIES

    @property
    def retry_interval(self):
        """
        Returns the interval one has to wait before retrying to connect.
        :return: The interval before retrying.
        """
        return DHT_TRACKER_RECHECK_INTERVAL

    @property
    def last_contact(self):
        # we never want this session to be cleaned up as it's faker than a 4 eur bill.
        return time.time()
