from __future__ import absolute_import

import json
from binascii import unhexlify
from random import randint, sample

from twisted.internet import reactor
from twisted.web import http, resource
from twisted.web.server import NOT_DONE_YET

import Tribler.Test.GUI.FakeTriblerAPI.tribler_utils as tribler_utils


class MetadataEndpoint(resource.Resource):

    def __init__(self):
        resource.Resource.__init__(self)

        child_handler_dict = {
            "channels": ChannelsEndpoint,
            "torrents": TorrentsEndpoint
        }

        for path, child_cls in child_handler_dict.items():
            self.putChild(path, child_cls())


class BaseChannelsEndpoint(resource.Resource):

    @staticmethod
    def return_404(request, message="the channel with the provided cid is not known"):
        """
        Returns a 404 response code if your channel has not been created.
        """
        request.setResponseCode(http.NOT_FOUND)
        return json.dumps({"error": message})


class ChannelsEndpoint(BaseChannelsEndpoint):

    def __init__(self):
        BaseChannelsEndpoint.__init__(self)

        child_handler_dict = {
            "popular": ChannelsPopularEndpoint
        }
        for path, child_cls in child_handler_dict.items():
            self.putChild(path, child_cls())

    def getChild(self, path, request):
        if path == "popular":
            return ChannelsPopularEndpoint()

        return ChannelPublicKeyEndpoint(path)

    @staticmethod
    def sanitize_parameters(parameters):
        """
        Sanitize the parameters and check whether they exist
        """
        first = 1 if 'first' not in parameters else int(parameters['first'][0])  # TODO check integer!
        last = 50 if 'last' not in parameters else int(parameters['last'][0])  # TODO check integer!
        sort_by = None if 'sort_by' not in parameters else parameters['sort_by'][0]  # TODO check integer!
        sort_asc = True if 'sort_asc' not in parameters else bool(int(parameters['sort_asc'][0]))
        query_filter = None if 'filter' not in parameters else parameters['filter'][0]

        if query_filter:
            parts = query_filter.split("\"")
            query_filter = parts[1]

        subscribed = False
        if 'subscribed' in parameters:
            subscribed = bool(int(parameters['subscribed'][0]))

        return first, last, sort_by, sort_asc, query_filter, subscribed

    def render_GET(self, request):
        first, last, sort_by, sort_asc, query_filter, subscribed = ChannelsEndpoint.sanitize_parameters(request.args)
        channels, total = tribler_utils.tribler_data.get_channels(first, last, sort_by, sort_asc, query_filter,
                                                                  subscribed)
        return json.dumps({
            "results": channels,
            "first": first,
            "last": last,
            "sort_by": sort_by,
            "sort_asc": int(sort_asc),
            "total": total
        })


class ChannelPublicKeyEndpoint(BaseChannelsEndpoint):

    def getChild(self, path, request):
        return SpecificChannelEndpoint(self.channel_pk, path)

    def __init__(self, path):
        BaseChannelsEndpoint.__init__(self)
        self.channel_pk = unhexlify(path)


class SpecificChannelEndpoint(resource.Resource):

    def __init__(self, channel_pk, path):
        resource.Resource.__init__(self)
        self.channel_pk = channel_pk
        self.channel_id = int(path)

        self.putChild(b"torrents", SpecificChannelTorrentsEndpoint(self.channel_pk, self.channel_id))

    def render_POST(self, request):
        parameters = http.parse_qs(request.content.read(), 1)
        if 'subscribe' not in parameters:
            request.setResponseCode(http.BAD_REQUEST)
            return json.dumps({"success": False, "error": "subscribe parameter missing"})

        to_subscribe = bool(int(parameters['subscribe'][0]))
        channel = tribler_utils.tribler_data.get_channel_with_public_key(self.channel_pk)
        if channel is None:
            return BaseChannelsEndpoint.return_404(request)

        if to_subscribe:
            tribler_utils.tribler_data.subscribed_channels.add(channel.id)
            channel.subscribed = True
        else:
            if channel.id in tribler_utils.tribler_data.subscribed_channels:
                tribler_utils.tribler_data.subscribed_channels.remove(channel.id)
            channel.subscribed = False

        return json.dumps({"success": True})


class SpecificChannelTorrentsEndpoint(BaseChannelsEndpoint):

    def __init__(self, channel_pk, channel_id):
        BaseChannelsEndpoint.__init__(self)
        self.channel_pk = channel_pk
        self.channel_id = channel_id

    @staticmethod
    def sanitize_parameters(parameters):
        """
        Sanitize the parameters and check whether they exist
        """
        first = 1 if 'first' not in parameters else int(parameters['first'][0])  # TODO check integer!
        last = 50 if 'last' not in parameters else int(parameters['last'][0])  # TODO check integer!
        sort_by = None if 'sort_by' not in parameters else parameters['sort_by'][0]  # TODO check integer!
        sort_asc = True if 'sort_asc' not in parameters else bool(int(parameters['sort_asc'][0]))
        query_filter = None if 'filter' not in parameters else parameters['filter'][0]

        channel = ''
        if 'channel' in parameters:
            channel = unhexlify(parameters['channel'][0])

        if query_filter:
            parts = query_filter.split("\"")
            query_filter = parts[1]

        return first, last, sort_by, sort_asc, query_filter, channel

    def render_GET(self, request):
        first, last, sort_by, sort_asc, query_filter, channel = SpecificChannelTorrentsEndpoint.sanitize_parameters(
            request.args)
        channel_obj = tribler_utils.tribler_data.get_channel_with_public_key(self.channel_pk)
        if not channel_obj:
            return SpecificChannelTorrentsEndpoint.return_404(request)

        torrents, total = tribler_utils.tribler_data.get_torrents(first, last, sort_by, sort_asc, query_filter, channel)
        return json.dumps({
            "results": torrents,
            "first": first,
            "last": last,
            "sort_by": sort_by,
            "sort_asc": int(sort_asc),
            "total": total
        })


class ChannelsPopularEndpoint(BaseChannelsEndpoint):

    def render_GET(self, _request):
        results_json = [channel.get_json() for channel in sample(tribler_utils.tribler_data.channels, 20)]
        return json.dumps({"channels": results_json})


class TorrentsEndpoint(resource.Resource):

    def getChild(self, path, request):
        if path == "random":
            return TorrentsRandomEndpoint()

        return SpecificTorrentEndpoint(path)


class TorrentsRandomEndpoint(resource.Resource):

    def render_GET(self, _request):
        return json.dumps({"torrents": [torrent.get_json()
                                        for torrent in sample(tribler_utils.tribler_data.torrents, 20)]})


class SpecificTorrentEndpoint(resource.Resource):

    def __init__(self, infohash):
        resource.Resource.__init__(self)
        self.infohash = unhexlify(infohash)

        self.putChild("health", SpecificTorrentHealthEndpoint(self.infohash))

    def render_GET(self, request):
        torrent = tribler_utils.tribler_data.get_torrent_with_infohash(self.infohash)
        if not torrent:
            request.setResponseCode(http.NOT_FOUND)
            return json.dumps({"error": "the torrent with the specific infohash cannot be found"})

        return json.dumps({"torrent": torrent.get_json(include_trackers=True)})


class SpecificTorrentHealthEndpoint(resource.Resource):

    def __init__(self, infohash):
        resource.Resource.__init__(self)
        self.infohash = infohash

    def render_GET(self, request):
        torrent = tribler_utils.tribler_data.get_torrent_with_infohash(self.infohash)
        if not torrent:
            request.setResponseCode(http.NOT_FOUND)
            return json.dumps({"error": "the torrent with the specific infohash cannot be found"})

        def update_health():
            if not request.finished:
                torrent.update_health()
                request.write(json.dumps({
                    "health": {
                        "DHT": {
                            "seeders": torrent.num_seeders,
                            "leechers": torrent.num_leechers
                        }
                    }
                }))
                request.finish()

        reactor.callLater(randint(0, 5), update_health)

        return NOT_DONE_YET
