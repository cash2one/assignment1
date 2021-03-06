"""Functions that read and write gzipped files.

The user of the file doesn't have to worry about the compression,
but random access is not allowed."""

# based on Andrew Kuchling's minigzip.py distributed with the zlib module

# SP3RDPARTY : Python standard library : Python License

import struct, sys, time
import zlib
import io
import subprocess
import __builtin__

__all__ = ["Reader", "Writer"]

# DJM - Python's included GzipFile I/O filter is really, really
# slow. Slower than writing the entire file flat and then compressing
# it with gzip afterward. Spawning gzip as a subprocess is
# faster. This actually runs concurrently with the parent Python
# process, so it's actually a parallelism gain!

def Reader(filename):
    return subprocess.Popen(['gunzip', '-c', filename],
                            bufsize=0, # bufsize >0 slows things down
                            stdout=subprocess.PIPE).stdout

def WriterProcess(fileobj):
    compresslevel = 6
    return subprocess.Popen(['gzip', '-%d' % compresslevel, '-c'],
                            bufsize=1*1024*1024, # want a largish buffer so the processes don't ping-pong
                            stdin=subprocess.PIPE, stdout=fileobj)
def Writer(fileobj):
    return WriterProcess(fileobj).stdin

# DJM - this is supposed to be faster, but isn't :(

FTEXT, FHCRC, FEXTRA, FNAME, FCOMMENT = 1, 2, 4, 8, 16

READ, WRITE = 1, 2

def write32u(output, value):
    # The L format writes the bit pattern correctly whether signed
    # or unsigned.
    output.write(struct.pack("<L", value))

def read32(input):
    return struct.unpack("<I", input.read(4))[0]

class FastGzipFilePYTHON(io.BufferedIOBase):
    """The GzipFile class simulates most of the methods of a file object with
    the exception of the readinto() and truncate() methods.

    """

    myfileobj = None
    max_read_chunk = 10 * 1024 * 1024   # 10Mb

    def __init__(self, filename=None, mode=None,
                 compresslevel=9, fileobj=None):
        """Constructor for the GzipFile class.

        At least one of fileobj and filename must be given a
        non-trivial value.

        The new class instance is based on fileobj, which can be a regular
        file, a StringIO object, or any other object which simulates a file.
        It defaults to None, in which case filename is opened to provide
        a file object.

        When fileobj is not None, the filename argument is only used to be
        included in the gzip file header, which may includes the original
        filename of the uncompressed file.  It defaults to the filename of
        fileobj, if discernible; otherwise, it defaults to the empty string,
        and in this case the original filename is not included in the header.

        The mode argument can be any of 'r', 'rb', 'a', 'ab', 'w', or 'wb',
        depending on whether the file will be read or written.  The default
        is the mode of fileobj if discernible; otherwise, the default is 'rb'.
        Be aware that only the 'rb', 'ab', and 'wb' values should be used
        for cross-platform portability.

        The compresslevel argument is an integer from 1 to 9 controlling the
        level of compression; 1 is fastest and produces the least compression,
        and 9 is slowest and produces the most compression.  The default is 9.

        """

        # guarantee the file is opened in binary mode on platforms
        # that care about that sort of thing
        if mode and 'b' not in mode:
            mode += 'b'
        if fileobj is None:
            fileobj = self.myfileobj = __builtin__.open(filename, mode or 'rb')
        if filename is None:
            if hasattr(fileobj, 'name'): filename = fileobj.name
            else: filename = ''
        if mode is None:
            if hasattr(fileobj, 'mode'): mode = fileobj.mode
            else: mode = 'rb'

        if mode[0:1] == 'r':
            self.mode = READ
            # Set flag indicating start of a new member
            self._new_member = True
            # Buffer data read from gzip file. extrastart is offset in
            # stream where buffer starts. extrasize is number of
            # bytes remaining in buffer from current stream position.
            self.extrabuf = ""
            self.extrasize = 0
            self.extrastart = 0
            self.name = filename
            # Starts small, scales exponentially
            self.min_readsize = 100

        elif mode[0:1] == 'w' or mode[0:1] == 'a':
            self.mode = WRITE
            self._init_write(filename)
            self.compress = zlib.compressobj(compresslevel,
                                             zlib.DEFLATED,
                                             -zlib.MAX_WBITS,
                                             zlib.DEF_MEM_LEVEL,
                                             0)
        else:
            raise IOError, "Mode " + mode + " not supported"

        self.fileobj = fileobj
        self.offset = 0

        if self.mode == WRITE:
            self._write_gzip_header()

    @property
    def filename(self):
        import warnings
        warnings.warn("use the name attribute", DeprecationWarning, 2)
        if self.mode == WRITE and self.name[-3:] != ".gz":
            return self.name + ".gz"
        return self.name

    def __repr__(self):
        s = repr(self.fileobj)
        return '<gzip ' + s[1:-1] + ' ' + hex(id(self)) + '>'

    def _init_write(self, filename):
        self.name = filename
        self.crc = zlib.crc32("") & 0xffffffffL
        self.size = 0
        self.writebuf = []
        self.bufsize = 0

    def _write_gzip_header(self):
        self.fileobj.write('\037\213')             # magic header
        self.fileobj.write('\010')                 # compression method
        fname = self.name
        if fname.endswith(".gz"):
            fname = fname[:-3]
        flags = 0
        if fname:
            flags = FNAME
        self.fileobj.write(chr(flags))
        write32u(self.fileobj, long(time.time()))
        self.fileobj.write('\002')
        self.fileobj.write('\377')
        if fname:
            self.fileobj.write(fname + '\000')

    def _init_read(self):
        self.crc = zlib.crc32("") & 0xffffffffL
        self.size = 0

    def _read_gzip_header(self):
        magic = self.fileobj.read(2)
        if magic != '\037\213':
            raise IOError, 'Not a gzipped file'
        method = ord( self.fileobj.read(1) )
        if method != 8:
            raise IOError, 'Unknown compression method'
        flag = ord( self.fileobj.read(1) )
        # modtime = self.fileobj.read(4)
        # extraflag = self.fileobj.read(1)
        # os = self.fileobj.read(1)
        self.fileobj.read(6)

        if flag & FEXTRA:
            # Read & discard the extra field, if present
            xlen = ord(self.fileobj.read(1))
            xlen = xlen + 256*ord(self.fileobj.read(1))
            self.fileobj.read(xlen)
        if flag & FNAME:
            # Read and discard a null-terminated string containing the filename
            while True:
                s = self.fileobj.read(1)
                if not s or s=='\000':
                    break
        if flag & FCOMMENT:
            # Read and discard a null-terminated string containing a comment
            while True:
                s = self.fileobj.read(1)
                if not s or s=='\000':
                    break
        if flag & FHCRC:
            self.fileobj.read(2)     # Read & discard the 16-bit header CRC

    def write(self,data):
        if self.mode != WRITE:
            import errno
            raise IOError(errno.EBADF, "write() on read-only GzipFile object")

        if self.fileobj is None:
            raise ValueError, "write() on closed GzipFile object"

        # Convert data type if called by io.BufferedWriter.
        # DJM - memoryview is not in Python 2.6
        #if isinstance(data, memoryview):
        #    data = data.tobytes()

        if len(data) > 0:
            self.size = self.size + len(data)
            self.crc = zlib.crc32(data, self.crc) & 0xffffffffL
            self.fileobj.write( self.compress.compress(data) )
            self.offset += len(data)

        return len(data)

    def read(self, size=-1):
        if self.mode != READ:
            import errno
            raise IOError(errno.EBADF, "read() on write-only GzipFile object")

        if self.extrasize <= 0 and self.fileobj is None:
            return ''

        readsize = 1024
        if size < 0:        # get the whole thing
            try:
                while True:
                    self._read(readsize)
                    readsize = min(self.max_read_chunk, readsize * 2)
            except EOFError:
                size = self.extrasize
        else:               # just get some more of it
            try:
                while size > self.extrasize:
                    self._read(readsize)
                    readsize = min(self.max_read_chunk, readsize * 2)
            except EOFError:
                if size > self.extrasize:
                    size = self.extrasize

        offset = self.offset - self.extrastart
        chunk = self.extrabuf[offset: offset + size]
        self.extrasize = self.extrasize - size

        self.offset += size
        return chunk

    def _unread(self, buf):
        self.extrasize = len(buf) + self.extrasize
        self.offset -= len(buf)

    def _read(self, size=1024):
        if self.fileobj is None:
            raise EOFError, "Reached EOF"

        if self._new_member:
            # If the _new_member flag is set, we have to
            # jump to the next member, if there is one.
            #
            # First, check if we're at the end of the file;
            # if so, it's time to stop; no more members to read.
            pos = self.fileobj.tell()   # Save current position
            self.fileobj.seek(0, 2)     # Seek to end of file
            if pos == self.fileobj.tell():
                raise EOFError, "Reached EOF"
            else:
                self.fileobj.seek( pos ) # Return to original position

            self._init_read()
            self._read_gzip_header()
            self.decompress = zlib.decompressobj(-zlib.MAX_WBITS)
            self._new_member = False

        # Read a chunk of data from the file
        buf = self.fileobj.read(size)

        # If the EOF has been reached, flush the decompression object
        # and mark this object as finished.

        if buf == "":
            uncompress = self.decompress.flush()
            self._read_eof()
            self._add_read_data( uncompress )
            raise EOFError, 'Reached EOF'

        uncompress = self.decompress.decompress(buf)
        self._add_read_data( uncompress )

        if self.decompress.unused_data != "":
            # Ending case: we've come to the end of a member in the file,
            # so seek back to the start of the unused data, finish up
            # this member, and read a new gzip header.
            # (The number of bytes to seek back is the length of the unused
            # data, minus 8 because _read_eof() will rewind a further 8 bytes)
            self.fileobj.seek( -len(self.decompress.unused_data)+8, 1)

            # Check the CRC and file size, and set the flag so we read
            # a new member on the next call
            self._read_eof()
            self._new_member = True

    def _add_read_data(self, data):
        self.crc = zlib.crc32(data, self.crc) & 0xffffffffL
        offset = self.offset - self.extrastart
        self.extrabuf = self.extrabuf[offset:] + data
        self.extrasize = self.extrasize + len(data)
        self.extrastart = self.offset
        self.size = self.size + len(data)

    def _read_eof(self):
        # We've read to the end of the file, so we have to rewind in order
        # to reread the 8 bytes containing the CRC and the file size.
        # We check the that the computed CRC and size of the
        # uncompressed data matches the stored values.  Note that the size
        # stored is the true file size mod 2**32.
        self.fileobj.seek(-8, 1)
        crc32 = read32(self.fileobj)
        isize = read32(self.fileobj)  # may exceed 2GB
        if crc32 != self.crc:
            raise IOError("CRC check failed %s != %s" % (hex(crc32),
                                                         hex(self.crc)))
        elif isize != (self.size & 0xffffffffL):
            raise IOError, "Incorrect length of data produced"

    @property
    def closed(self):
        return self.fileobj is None

    def close(self):
        if self.fileobj is None:
            return
        if self.mode == WRITE:
            self.fileobj.write(self.compress.flush())
            write32u(self.fileobj, self.crc)
            # self.size may exceed 2GB, or even 4GB
            write32u(self.fileobj, self.size & 0xffffffffL)
            self.fileobj = None
        elif self.mode == READ:
            self.fileobj = None
        if self.myfileobj:
            self.myfileobj.close()
            self.myfileobj = None

    def flush(self,zlib_mode=zlib.Z_SYNC_FLUSH):
        if self.mode == WRITE:
            # Ensure the compressor's buffer is flushed
            self.fileobj.write(self.compress.flush(zlib_mode))
            self.fileobj.flush()

    def fileno(self):
        """Invoke the underlying file object's fileno() method.

        This will raise AttributeError if the underlying file object
        doesn't support fileno().
        """
        return self.fileobj.fileno()

    def rewind(self):
        '''Return the uncompressed stream file position indicator to the
        beginning of the file'''
        if self.mode != READ:
            raise IOError("Can't rewind in write mode")
        self.fileobj.seek(0)
        self._new_member = True
        self.extrabuf = ""
        self.extrasize = 0
        self.extrastart = 0
        self.offset = 0

    def readable(self):
        return self.mode == READ

    def writable(self):
        return self.mode == WRITE

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        if whence:
            if whence == 1:
                offset = self.offset + offset
            else:
                raise ValueError('Seek from end not supported')
        if self.mode == WRITE:
            if offset < self.offset:
                raise IOError('Negative seek in write mode')
            count = offset - self.offset
            for i in range(count // 1024):
                self.write(1024 * '\0')
            self.write((count % 1024) * '\0')
        elif self.mode == READ:
            if offset < self.offset:
                # for negative seek, rewind and do positive seek
                self.rewind()
            count = offset - self.offset
            for i in range(count // 1024):
                self.read(1024)
            self.read(count % 1024)

        return self.offset

    def readline(self, size=-1):
        if size < 0:
            # Shortcut common case - newline found in buffer.
            offset = self.offset - self.extrastart
            i = self.extrabuf.find('\n', offset) + 1
            if i > 0:
                self.extrasize -= i - offset
                self.offset += i - offset
                return self.extrabuf[offset: i]

            size = sys.maxint
            readsize = self.min_readsize
        else:
            readsize = size
        bufs = []
        while size != 0:
            c = self.read(readsize)
            i = c.find('\n')

            # We set i=size to break out of the loop under two
            # conditions: 1) there's no newline, and the chunk is
            # larger than size, or 2) there is a newline, but the
            # resulting line would be longer than 'size'.
            if (size <= i) or (i == -1 and len(c) > size):
                i = size - 1

            if i >= 0 or c == '':
                bufs.append(c[:i + 1])    # Add portion of last chunk
                self._unread(c[i + 1:])   # Push back rest of chunk
                break

            # Append chunk to list, decrease 'size',
            bufs.append(c)
            size = size - len(c)
            readsize = min(size, readsize * 2)
        if readsize > self.min_readsize:
            self.min_readsize = min(readsize, self.min_readsize * 2, 512)
        return ''.join(bufs) # Return resulting line
