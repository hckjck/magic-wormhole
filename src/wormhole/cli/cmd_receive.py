from __future__ import print_function
import os, sys, json, binascii, six, tempfile, zipfile
from tqdm import tqdm
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import log
from ..wormhole import wormhole
from ..transit import TransitReceiver
from ..errors import TransferError, WormholeClosedError

APPID = u"lothar.com/wormhole/text-or-file-xfer"

class RespondError(Exception):
    def __init__(self, response):
        self.response = response

def receive(args, reactor=reactor):
    """I implement 'wormhole receive'. I return a Deferred that fires with
    None (for success), or signals one of the following errors:
    * WrongPasswordError: the two sides didn't use matching passwords
    * Timeout: something didn't happen fast enough for our tastes
    * TransferError: the sender rejected the transfer: verifier mismatch
    * any other error: something unexpected happened
    """
    return TwistedReceiver(args, reactor).go()


class TwistedReceiver:
    def __init__(self, args, reactor=reactor):
        assert isinstance(args.relay_url, type(u""))
        self.args = args
        self._reactor = reactor
        self._tor_manager = None
        self._transit_receiver = None

    def msg(self, *args, **kwargs):
        print(*args, file=self.args.stdout, **kwargs)

    @inlineCallbacks
    def go(self):
        if self.args.tor:
            with self.args.timing.add("import", which="tor_manager"):
                from ..tor_manager import TorManager
            self._tor_manager = TorManager(self._reactor,
                                           timing=self.args.timing)
            # For now, block everything until Tor has started. Soon: launch
            # tor in parallel with everything else, make sure the TorManager
            # can lazy-provide an endpoint, and overlap the startup process
            # with the user handing off the wormhole code
            yield self._tor_manager.start()

        w = wormhole(APPID, self.args.relay_url, self._reactor,
                     self._tor_manager, timing=self.args.timing)
        # I wanted to do this instead:
        #
        #    try:
        #        yield self._go(w, tor_manager)
        #    finally:
        #        yield w.close()
        #
        # but when _go had a UsageError, the stacktrace was always displayed
        # as coming from the "yield self._go" line, which wasn't very useful
        # for tracking it down.
        d = self._go(w)
        d.addBoth(w.close)
        yield d

    @inlineCallbacks
    def _go(self, w):
        yield self.handle_code(w)
        verifier = yield w.verify()
        self.show_verifier(verifier)

        want_offer = True
        done = False

        while True:
            try:
                them_d = yield self._get_data(w)
            except WormholeClosedError:
                if done:
                    returnValue(None)
                raise TransferError("unexpected close")
            #print("GOT", them_d)
            if u"transit" in them_d:
                yield self._parse_transit(them_d[u"transit"], w)
                continue
            if u"offer" in them_d:
                if not want_offer:
                    raise TransferError("duplicate offer")
                try:
                    yield self.parse_offer(them_d[u"offer"], w)
                except RespondError as r:
                    self._send_data({"error": r.response}, w)
                    raise TransferError(r.response)
                returnValue(None)
            log.msg("unrecognized message %r" % (them_d,))
            raise TransferError("expected offer, got none")

    def _send_data(self, data, w):
        data_bytes = json.dumps(data).encode("utf-8")
        w.send(data_bytes)

    @inlineCallbacks
    def _get_data(self, w):
        # this may raise WrongPasswordError
        them_bytes = yield w.get()
        them_d = json.loads(them_bytes.decode("utf-8"))
        if "error" in them_d:
            raise TransferError(them_d["error"])
        returnValue(them_d)

    @inlineCallbacks
    def handle_code(self, w):
        code = self.args.code
        if self.args.zeromode:
            assert not code
            code = u"0-"
        if code:
            w.set_code(code)
        else:
            yield w.input_code("Enter receive wormhole code: ",
                               self.args.code_length)

    def show_verifier(self, verifier):
        verifier_hex = binascii.hexlify(verifier).decode("ascii")
        if self.args.verify:
            self.msg(u"Verifier %s." % verifier_hex)

    @inlineCallbacks
    def _parse_transit(self, sender_hints, w):
        if self._transit_receiver:
            # TODO: accept multiple messages, add the additional hints to the
            # existing TransitReceiver
            return
        yield self._build_transit(w, sender_hints)

    @inlineCallbacks
    def _build_transit(self, w, sender_hints):
        tr = TransitReceiver(self.args.transit_helper,
                             no_listen=self.args.no_listen,
                             tor_manager=self._tor_manager,
                             reactor=self._reactor,
                             timing=self.args.timing)
        self._transit_receiver = tr
        transit_key = w.derive_key(APPID+u"/transit-key", tr.TRANSIT_KEY_LENGTH)
        tr.set_transit_key(transit_key)

        tr.add_their_direct_hints(sender_hints["direct_connection_hints"])
        tr.add_their_relay_hints(sender_hints["relay_connection_hints"])

        direct_hints = yield tr.get_direct_hints()
        relay_hints = yield tr.get_relay_hints()
        receiver_hints = {
            "direct_connection_hints": direct_hints,
            "relay_connection_hints": relay_hints,
            }
        self._send_data({u"transit": receiver_hints}, w)
        # TODO: send more hints as the TransitReceiver produces them

    @inlineCallbacks
    def parse_offer(self, them_d, w):
        if "message" in them_d:
            self.handle_text(them_d, w)
            returnValue(None)
        # transit will be created by this point, but not connected
        if "file" in them_d:
            f = self.handle_file(them_d)
            self._send_permission(w)
            rp = yield self._establish_transit()
            yield self._transfer_data(rp, f)
            self.write_file(f)
            yield self.close_transit(rp)
        elif "directory" in them_d:
            f = self.handle_directory(them_d)
            self._send_permission(w)
            rp = yield self._establish_transit()
            yield self._transfer_data(rp, f)
            self.write_directory(f)
            yield self.close_transit(rp)
        else:
            self.msg(u"I don't know what they're offering\n")
            self.msg(u"Offer details: %r" % (them_d,))
            raise RespondError("unknown offer type")

    def handle_text(self, them_d, w):
        # we're receiving a text message
        self.msg(them_d["message"])
        self._send_data({"answer": {"message_ack": "ok"}}, w)

    def handle_file(self, them_d):
        file_data = them_d["file"]
        self.abs_destname = self.decide_destname("file",
                                                 file_data["filename"])
        self.xfersize = file_data["filesize"]

        self.msg(u"Receiving file (%d bytes) into: %s" %
                 (self.xfersize, os.path.basename(self.abs_destname)))
        self.ask_permission()
        tmp_destname = self.abs_destname + ".tmp"
        return open(tmp_destname, "wb")

    def handle_directory(self, them_d):
        file_data = them_d["directory"]
        zipmode = file_data["mode"]
        if zipmode != "zipfile/deflated":
            self.msg(u"Error: unknown directory-transfer mode '%s'" % (zipmode,))
            raise RespondError("unknown mode")
        self.abs_destname = self.decide_destname("directory",
                                                 file_data["dirname"])
        self.xfersize = file_data["zipsize"]

        self.msg(u"Receiving directory (%d bytes) into: %s/" %
                 (self.xfersize, os.path.basename(self.abs_destname)))
        self.msg(u"%d files, %d bytes (uncompressed)" %
                 (file_data["numfiles"], file_data["numbytes"]))
        self.ask_permission()
        return tempfile.SpooledTemporaryFile()

    def decide_destname(self, mode, destname):
        # the basename() is intended to protect us against
        # "~/.ssh/authorized_keys" and other attacks
        destname = os.path.basename(destname)
        if self.args.output_file:
            destname = self.args.output_file # override
        abs_destname = os.path.join(self.args.cwd, destname)

        # get confirmation from the user before writing to the local directory
        if os.path.exists(abs_destname):
            self.msg(u"Error: refusing to overwrite existing %s %s" %
                     (mode, destname))
            raise RespondError("%s already exists" % mode)
        return abs_destname

    def ask_permission(self):
        with self.args.timing.add("permission", waiting="user") as t:
            while True and not self.args.accept_file:
                ok = six.moves.input("ok? (y/n): ")
                if ok.lower().startswith("y"):
                    break
                print(u"transfer rejected", file=sys.stderr)
                t.detail(answer="no")
                raise RespondError("transfer rejected")
            t.detail(answer="yes")

    def _send_permission(self, w):
        self._send_data({"answer": { "file_ack": "ok" }}, w)

    @inlineCallbacks
    def _establish_transit(self):
        record_pipe = yield self._transit_receiver.connect()
        self.args.timing.add("transit connected")
        returnValue(record_pipe)

    @inlineCallbacks
    def _transfer_data(self, record_pipe, f):
        # now receive the rest of the owl
        self.msg(u"Receiving (%s).." % record_pipe.describe())

        with self.args.timing.add("rx file"):
            progress = tqdm(file=self.args.stdout,
                            disable=self.args.hide_progress,
                            unit="B", unit_scale=True, total=self.xfersize)
            with progress:
                received = yield record_pipe.writeToFile(f, self.xfersize,
                                                         progress.update)

        # except TransitError
        if received < self.xfersize:
            self.msg()
            self.msg(u"Connection dropped before full file received")
            self.msg(u"got %d bytes, wanted %d" % (received, self.xfersize))
            raise TransferError("Connection dropped before full file received")
        assert received == self.xfersize

    def write_file(self, f):
        tmp_name = f.name
        f.close()
        os.rename(tmp_name, self.abs_destname)
        self.msg(u"Received file written to %s" %
                 os.path.basename(self.abs_destname))

    def write_directory(self, f):
        self.msg(u"Unpacking zipfile..")
        with self.args.timing.add("unpack zip"):
            with zipfile.ZipFile(f, "r", zipfile.ZIP_DEFLATED) as zf:
                zf.extractall(path=self.abs_destname)
                # extractall() appears to offer some protection against
                # malicious pathnames. For example, "/tmp/oops" and
                # "../tmp/oops" both do the same thing as the (safe)
                # "tmp/oops".
            self.msg(u"Received files written to %s/" %
                     os.path.basename(self.abs_destname))
            f.close()

    @inlineCallbacks
    def close_transit(self, record_pipe):
        with self.args.timing.add("send ack"):
            yield record_pipe.send_record(b"ok\n")
            yield record_pipe.close()
