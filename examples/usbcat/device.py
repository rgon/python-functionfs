#!/usr/bin/env python -u
from __future__ import print_function
import errno
import fcntl
import os
import select
import sys
import functionfs
import functionfs.ch9
import libaio

# More than one, so we may process one while kernel fills the other.
PENDING_READ_COUNT = 2
# Large-ish buffer, to tolerate bursts without becoming a context switch storm.
BUF_SIZE = 1024 * 1024

class USBCat(functionfs.Function):
    _enabled = False

    def __init__(self, path, writer):
        self._aio_context = libaio.AIOContext(PENDING_READ_COUNT)
        self.eventfd = eventfd = libaio.EventFD()
        self._writer = writer
        fs_list, hs_list, ss_list = functionfs.getInterfaceInAllSpeeds(
            interface={
                'bInterfaceClass': functionfs.ch9.USB_CLASS_VENDOR_SPEC,
                'iInterface': 1,
            },
            endpoint_list=[
                {
                    'endpoint': {
                        'bEndpointAddress': 1 | functionfs.ch9.USB_DIR_IN,
                        'bmAttributes': functionfs.ch9.USB_ENDPOINT_XFER_BULK,
                    },
                }, {
                    'endpoint': {
                        'bEndpointAddress': 2 | functionfs.ch9.USB_DIR_OUT,
                        'bmAttributes': functionfs.ch9.USB_ENDPOINT_XFER_BULK,
                    },
                },
            ],
        )
        super(USBCat, self).__init__(
            path,
            fs_list=fs_list,
            hs_list=hs_list,
            ss_list=ss_list,
            lang_dict={
                0x0409: [
                    u"USBCat",
                ],
            }
        )
        to_host = self.getEndpoint(2)
        self._aio_recv_block_list = [
            libaio.AIOBlock(
                libaio.AIOBLOCK_MODE_READ,
                to_host,
                [bytearray(BUF_SIZE)],
                0,
                eventfd,
            )
            for _ in xrange(PENDING_READ_COUNT)
        ]
        self.write = self.getEndpoint(1).write

    def close(self):
        super(USBCat, self).close()
        self._aio_context.close()

    def onUnbind(self):
        """
        Kernel may unbind us without calling disable, so call it ourselves to
        cancel AIO operation blocks.
        """
        self.onDisable()

    def onEnable(self):
        """
        The configuration containing this function has been enabled by host.
        Endpoints become working files, so submit some read operations.
        """
        if self._enabled:
            self.onDisable()
        self._aio_context.submit(self._aio_recv_block_list)
        self._enabled = True
        print('enabled', file=sys.stderr)

    def onDisable(self):
        """
        The configuration containing this function has been disabled by host.
        Endpoint do not work anymore, so cancel AIO operation blocks.
        """
        if self._enabled:
            for block in self._aio_recv_block_list:
                self._aio_context.cancel(block)
            self._enabled = False
            print('disabled', file=sys.stderr)

    def readAIOCompletion(self):
        """
        Call when eventfd notified events are available.
        """
        event_count = self.eventfd.read()
        print('eventfd reorts %i events' % event_count, file=sys.stderr)
        block_list = []
        for block, res, _ in self._aio_context.getEvents(event_count):
            if res != -errno.ESHUTDOWN:
                block_list.append(block)
            if res < 0:
                print('error:', res, file=sys.stderr)
            else:
                print('received', res, 'bytes', file=sys.stderr)
                buf, = block.buffer_list
                self._writer(buf[:res])
        self._aio_context.submit(block_list)

def main(path):
    with USBCat(
        path,
        sys.stdout.write,
    ) as function:
        fcntl.fcntl(
            sys.stdin,
            fcntl.F_SETFL,
            fcntl.fcntl(sys.stdin, fcntl.F_GETFL) | os.O_NONBLOCK,
        )
        def sender():
            buf = sys.stdin.read(BUF_SIZE)
            print('sending', len(buf), 'bytes', file=sys.stderr)
            function.write(buf)
        epoll = select.epoll(3)
        event_dispatcher_dict = {}
        def register(file_object, handler):
            epoll.register(file_object, select.EPOLLIN)
            event_dispatcher_dict[file_object.fileno()] = handler
        def noIntrEpoll():
            while True:
                try:
                    return epoll.poll()
                except IOError, exc:
                    if exc.errno != errno.EINTR:
                        raise
        register(function.eventfd, function.readAIOCompletion)
        register(function.ep0, function.processEvents)
        register(sys.stdin, sender)
        try:
            while True:
                for fd, event in noIntrEpoll():
                    print(
                        'epoll: fd %r got event %r' % (fd, event),
                        file=sys.stderr,
                    )
                    event_dispatcher_dict[fd]()
        except (KeyboardInterrupt, EOFError):
            pass

if __name__ == '__main__':
    main(*sys.argv[1:])
