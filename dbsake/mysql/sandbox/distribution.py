"""
dbsake.mysql.sandbox.distribution
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Support for deploying MySQL distributions

"""

from __future__ import print_function

import collections
import contextlib
import hashlib
import logging
import os
import tarfile
import time
import urllib2
import re
import shutil
import sys

from dbsake.thirdparty import sarge
from dbsake.util import path as dbsake_path

from . import common
from . import util

info = logging.info
debug = logging.debug
warn = logging.warn
error = logging.error

class MySQLVersion(collections.namedtuple('MySQLVersion', 'major minor release')):
    """Represent a MySQL version

    This class represents a MySQL version as a tuple of integer.  I.e. MySQL
    '5.6.15' is represented as (5, 6, 15).  This is used to make version
    comparisons easier.

    Typically instances are instantiated using the from_string class method

    >>> MySQLVersion.from_string('5.6.15')
    MySQLVersion(major=5, minor=6, release=15)
    >>> MySQLVersion.from_string('5.6.15') > (5, 5)
    True
    """
    def __str__(self):
        return '.'.join(str(part) for part in self)

    @classmethod
    def from_string(cls, value):
        value = value.partition('-')[0]
        return cls(*map(int, value.split('.')))

#: Represent a MySQL distribution
MySQLDistribution = collections.namedtuple('MySQLDistribution',
                                           ['version',
                                            'mysqld',
                                            'mysqld_safe',
                                            'mysql',
                                            'basedir',
                                            'sharedir',
                                            'libexecdir',
                                            'plugindir'])

def mysqld_version(mysqld):
    """Discover the MySQL version from a mysqld binary

    This method runs mysqld --version and extract out the
    version string to create a MySQLVersion tuple.  This is used
    to allow other portions of dbsake to check the mysql version
    in use and take appropriate action.  This is largely used by
    the generate_defaults method in dbsake.mysql.sandbox.common
    to conditionally enable my.cnf options based on the target
    MySQL version.

    :param mysqld: path to mysqld binary
    :returns: MySQLVersion instance
    """

    cmd = sarge.shell_format('{0} --version', mysqld)
    result = sarge.capture_both(cmd)
    if result.returncode != 0:
        error("    ! %s", result.stderr.text.rstrip())
        raise common.SandboxError("%s failed (exit status: %d)" %
                                  (cmd, result.returncode))
    m = re.search('(\d+[.]\d+[.]\d+)', result.stdout.text)
    if not m:
        raise common.SandboxError("Failed to discover version for %s" % cmd)
    return MySQLVersion.from_string(m.group(0))

# XXX this documentation isn't very clear
def first_subdir(basedir, *paths):
    """Return the first path from ``paths`` that exists under basedir

    :returns: first path that exists or None if no path was found
    """
    for name in paths:
        cpath = os.path.normpath(os.path.join(basedir, name))
        if os.path.exists(cpath):
            return cpath
    return None

def deploy(options):
    """Deploy a MySQL distribution

    This is the entry point to the distribution deployment API.  Currently
    this method supports either 'system', a tarball path or a mysql version
    string that facilitates downloading a tarball.

    'system' implies finding MySQL binaries and support files from common OS
    paths as provided by various distributions.  Binaries will typically be
    installed under /usr/bin, /usr/sbin or /usr/libexec.  Support files are
    often stored under /usr/share/mysql/.

    The tarball method expects a path to a binary distribution of MySQL and
    unpacks the tarball into the sandbox directory.

    The version method expects a version string along the lines of
    <major>.<minor>.<release>[-suffix] and will attempt to fetch the tarball
    on the user's behalf from cdn.mysql.com.  Aside from the download
    logic this is otherwise identical to the tarball method.

    :param options: SandboxOptions instance
    :returns: MySQLDistribution instance describing the distribution
    """

    start = time.time()
    try:
        if options.distribution == 'system':
            return distribution_from_system(options)
        elif os.path.isfile(options.distribution):
            return distribution_from_tarball(options)
        else:
            return distribution_from_download(options)
    finally:
        info("    * Deployed MySQL distribution to sandbox in %.2f seconds", time.time() - start)

def distribution_from_system(options):
    """Deploy a MySQL distribution already installed on the system

    """
    info("    - Deploying MySQL distributed from system binaries")
    envpath = os.pathsep.join(['/usr/libexec', '/usr/sbin', os.environ['PATH']])
    mysqld = dbsake_path.which('mysqld', path=envpath)
    mysql = dbsake_path.which('mysql', path=envpath)
    mysqld_safe = dbsake_path.which('mysqld_safe', path=envpath)

    if None in (mysqld, mysql, mysqld_safe):
        raise common.SandboxError("Unable to find MySQL binaries")
    info("    - Found mysqld: %s", mysqld)
    info("    - Found mysqld_safe: %s", mysqld_safe)
    info("    - Found mysql: %s", mysql)
    version = mysqld_version(mysqld)
    info("    - MySQL server version: %s", version)
    # XXX: we might be able to look this up from mysqld --help --verbose,
    #      but I really want to avoid that.  This shold cover 99% of local
    #      cases and I think it's fine to abort if this doesn't exist
    basedir = '/usr'
    info("    - MySQL --basedir %s", basedir)
    # sharedir is absolutely required as we need it to bootstrap mysql
    # and mysql will fail to start withtout it
    sharedir = first_subdir(basedir, 'share/mysql', 'share')
    if not sharedir:
        raise common.SandboxError("/usr/share/mysql not found")

    info("    - MySQL share found in %s", sharedir)
    # Note: plugindir may be None, if using mysql < 5.1
    plugindir = first_subdir(basedir, 'lib64/mysql/plugin', 'lib/mysql/plugin')
    if plugindir:
        info("    - Found MySQL plugin directory: %s", plugindir)

    # now copy mysqld, mysql, mysqld_safe to sandbox_dir/bin
    # then return an appropriate MySQLDistribution instance
    bindir = os.path.join(options.basedir, 'bin')
    dbsake_path.makedirs(bindir, 0770, exist_ok=True)
    for name in [mysqld]:
        shutil.copy2(name, bindir)
    info("    - Copied minimal MySQL commands to %s", bindir)
    return MySQLDistribution(
        version=version,
        mysqld=os.path.join(bindir, os.path.basename(mysqld)),
        mysqld_safe=mysqld_safe,
        mysql=mysql,
        basedir=basedir,
        sharedir=sharedir,
        libexecdir=bindir,
        plugindir=plugindir
    )


def unpack_tarball_distribution(stream, destdir):
    """Unpack a MySQL tar distribution in a directory

    This method filters several items from the tarball:
        - static libraries from ./lib/
        - *_embedded and mysqld-debug from ./bin/
        - ./mysql-test
        - ./sql-bench

    :param stream: stream of bytes from which the tarball data can be read
    :param destdir: destination directory files should be unpacked to
    """
    debug("    # unpacking tarball stream=%r destination=%r", stream, destdir)
    tar = tarfile.open(None, 'r|*', fileobj=stream)
    total_size = 0
    extracted_size = 0
    # python 2.6's tarfile does not support the context manager protocol
    # so try...finally is used here
    try:
        for tarinfo in tar:
            total_size += tarinfo.size
            if not (tarinfo.isreg() or tarinfo.issym()): continue
            name = os.path.normpath(tarinfo.name).partition(os.sep)[2]
            name0 = name.partition(os.sep)[0]
            if (name0 == 'bin' and
                not name.endswith('_embedded') and
                not name.endswith('mysqld-debug')) or \
               (name0 == 'lib' and not name.endswith('.a')) or \
               name0 == 'share':
                tarinfo.name = name
            elif name0 == 'scripts':
                tarinfo.name = os.path.join('bin', os.path.basename(name))
            elif name in ('COPYING', 'README', 'INSTALL-BINARY',
                          'docs/ChangeLog'):
                tarinfo.name = os.path.join('docs.mysql',
                                            os.path.basename(name))
            else:
                debug("    # Filtering: %s", name)
                continue
            # reset the user to something sane
            tarinfo.uname = 'mysql'
            tarinfo.group = 'mysql'
            tarinfo.uid = 0
            tarinfo.gid = 0
            # finally extract the element
            debug("    # Extracting: %s", name)
            tar.extract(tarinfo, destdir)
            extracted_size += tarinfo.size
    finally:
        tar.close()
        from dbsake.util import format_filesize
        info("    * Total tarball size: %s Extracted size: %s",
             format_filesize(total_size), format_filesize(extracted_size))

def distribution_from_tarball(options):
    """Deploy a MySQL distribution from a binary tarball

    """
    info("    - Deploying distribution from binary tarball: %s", options.distribution)
    with util.StreamProxy(open(options.distribution, 'rb')) as stream:
        if os.isatty(sys.stderr.fileno()):
            size = os.fstat(stream.fileno()).st_size
            stream.add(util.progressbar(max=size))
        unpack_tarball_distribution(stream, options.basedir)

    bindir = os.path.join(options.basedir, 'bin')
    version = mysqld_version(os.path.join(bindir, 'mysqld'))

    info("    - Using mysqld (v%s): %s", version, os.path.join(bindir, 'mysqld'))
    info("    - Using mysqld_safe: %s", os.path.join(bindir, 'mysqld_safe'))
    info("    - Using mysql: %s", os.path.join(bindir, 'mysql'))
    info("    - Using share directory: %s", os.path.join(options.basedir, 'share'))
    info("    - Using mysqld --basedir: %s", options.basedir)
    plugin_dir = os.path.join(options.basedir, 'lib', 'plugin')
    if os.path.exists(plugin_dir):
        info("    - Using MySQL plugin directory: %s", os.path.join(options.basedir, 'lib', 'plugin'))

    return MySQLDistribution(
        version=version,
        mysqld=os.path.join(bindir, 'mysqld'),
        mysql=os.path.join(bindir, 'mysql'),
        mysqld_safe=os.path.join(bindir, 'mysqld_safe'),
        basedir=options.basedir,
        sharedir=os.path.join(options.basedir, 'share'),
        libexecdir=bindir,
        plugindir=os.path.join(options.basedir, 'lib', 'plugin')
    )

class MySQLCDNInfo(collections.namedtuple("MySQLCDNInfo", "name locations")):
    """Encode information about the MySQL CDN

    This class provides a simple lookup table for expected tarball names and
    urls to try to fetch from in order to obtain these tarballs.

    This class is generally instantiated using the from_version class method

    To fetch a url, iterate over the instance which yields likely urls for
    various locations where a tarball might be found.
    """
    VERSIONS = {
        '5.0' : dict(
            name='mysql-{version}-linux-{arch}-glibc23.tar.gz',
            locations=(
                'archives/mysql-5.0/',
            )
        ),
        '5.1' : dict(
            name='mysql-{version}-linux-{arch}-glibc23.tar.gz',
            locations=(
                'Downloads/MySQL-5.1',
                'archives/mysql-5.1',
            )
        ),
        '5.5' : dict(
            name='mysql-{version}-linux2.6-{arch}.tar.gz',
            locations=(
                'Downloads/MySQL-5.5',
                'archives/mysql-5.5',
            )
        ),
        '5.6' : dict(
            name='mysql-{version}-linux-glibc2.5-{arch}.tar.gz',
            locations=(
                'Downloads/MySQL-5.6',
                'archives/mysql-5.6',
            )
        ),
        '5.7' : dict(
            name='mysql-{version}-linux-glibc2.5-{arch}.tar.gz',
            locations=(
                'Downloads/MySQL-5.7',
                'archives/get/file',
            )
        )
    }

    prefix = 'http://cdn.mysql.com'

    @classmethod
    def from_version(cls, version):
        major_minor = version.rpartition('.')[0]
        try:
            options = cls.VERSIONS[major_minor]
        except KeyError:
            raise common.SandboxError("Version '%s' is unsupported" % version)
        return cls(name=options['name'].format(version=version, arch='x86_64'),
                   locations=options['locations'])

    def __iter__(self):
        for path in self.locations:
            yield '/'.join([self.prefix, path, self.name])

def open_http_download(url):
    """Open a stream to tarball from cdn.mysql.com

    :param url: url to fetch from
    :returns: file-like object whose contents are a tarball
    """
    try:
        stream = urllib2.urlopen(url)
        # since this from cdn.mysql.com the etag encodes the md5sum
        # this is in "'md5sum:integer'" format
        etag = stream.headers['etag']
        checksum = etag[1:-1].rpartition(':')[0]
        stream.headers['x-dbsake-checksum'] = checksum
        return stream
    except urllib2.HTTPError as exc:
        if exc.code != 404:
            raise common.SandboxError("Failed download: %s" % exc)
        else:
            raise
    except urllib2.URLError as exc:
        raise common.SandboxError("Failed http download: %s" % exc)

def open_cached_download(path):
    """Open a stream to a cache binary tarball distribution

    This method finds the distribution specified by ``path`` and verifies
    that the stored md5sum exists and the stored file size matches. If
    the cached download looks incorrect a SandboxError is raised and
    allows a fallback to a network download.

    :param name: absolute path to the cached tarball
    :return: file-like object to cached download
    """
    # first check md5 information
    checksum = None
    length = None
    try:
        with open(path + '.md5', 'rb') as fileobj:
            for line in fileobj:
                if line.startswith('# size:'):
                    length = line.split()[-1]
                elif line.startswith('#'):
                    continue
                else:
                    checksum = line.split()[0]
    except IOError as exc:
        raise common.SandboxError("Invalid checksum for %s" % path)

    if checksum is None:
        raise common.SandboxError("No valid checksum found for cache file %s" % path)

    try:
        stream = urllib2.urlopen('file://' + path)
        if stream.headers['content-length'] != length:
            stream.close()
            debug("Cache size was not valid. Expected %s bytes found %s",
                  length, stream.headers['content-length'])
            raise common.SandboxError("Invalid cache file %s" % path)
        # set the expected checksum for later validation
        stream.headers['x-dbsake-checksum'] = checksum
        return stream
    except urllib2.URLError as exc:
        raise common.SandboxError("Invalid cache file %s" % path)

def discover_cache_path(name):
    """Discover the cache path for a base filename

    This computes the path from either the DBSAKE_CACHE environment variable
    and defaulting to ~/.dbsake/cache if DBSAKE_CACHE is not otherwise
    defined.

    :param name: base filename to cache
    :returns: absolute path name where the file should be written
    """
    # pull cache directory from environment and default to ~/.dbsake.cache
    cache_directory = os.environ.get('DBSAKE_CACHE', '~/.dbsake/cache')
    cache_path = os.path.join(cache_directory, name)
    # cleanup the cache directory by expanding ~ to $HOME and making
    # any relative paths absolute
    cache_path = os.path.abspath(os.path.expanduser(cache_path))
    # normalize the result - removing redundant directory separators, etc.
    return os.path.normpath(cache_path)

def download_mysql(version, arch, cache_policy):
    """Open a download stream for a MySQL binary tarball distribution

    :param version: version of MySQL to download
    :param arch: architecture to download for (should be either x86_64 or i686)
    :param cache_policy: how the download should be cached; one of:
                         'always', 'never', 'refresh', 'local'
    :returns: file-like object whose contents are a binary tarball
    :raises: SandboxError on error
    """
    cdn = MySQLCDNInfo.from_version(version)
    debug("    # Found MySQL CDN data: %r", cdn)

    cache_path = discover_cache_path(cdn.name)
    stream = None

    if cache_policy not in ('never', 'refresh'):
        # check if a file is in cache
        try:
            stream = open_cached_download(cache_path)
            info("    - Using cached download %s", cache_path)
        except common.SandboxError as exc:
            debug("stream_from_cache failed:", exc_info=True)
            if cache_policy == 'local':
                debug("Aborting. cache-policy = local and no usable cache file")
                raise
            debug("cache-policy allows network fallback, so continuing in spit of no cache")
            # otherwise fall through
            # distribution_from_cache should handle purging cache in this case

    if stream is None:
        debug("Attempting download of MySQL distribution")
        for url in cdn:
            debug(" Trying url: %s", url)
            try:
                stream = open_http_download(url)
                stream.info()['x-dbsake-cache'] = cache_path
                info("    - Downloading from %s", url)
            except urllib2.HTTPError as exc:
                if exc.code != 404:
                    raise common.SandboxError("Failed to download: %s" % exc)
                else:
                    continue
            except urllib2.URLError as exc:
                raise common.SandboxError("Failed to download: %s" % exc)
            else:
                break # stream was opened successfully

    if stream is None:
        raise common.SandboxError("No distribution found")

    if 'x-dbsake-cache' not in stream.info():
        stream.info()['x-dbsake-cache'] = ''
    return util.StreamProxy(stream)

@contextlib.contextmanager
def cache_download(name):
    """Cache a download in the specified path

    This is a context manager that provides a file object to write a cached
    download to.  This is used internally by the distribution_from_download
    method.

    :param name: path to write a cached download ot
    """
    dbsake_path.makedirs(os.path.dirname(name), exist_ok=True)
    with open(name, 'wb') as fileobj:
        yield fileobj

def check_for_libaio(options):
    """Verify that libaio is available, where necessary

    See http:/bugs.mysql.com/60544 for details of why this check is being done

    """
    version = MySQLVersion.from_string(options.distribution)
    if version < (5, 5, 4):
        return
    info("    - Checking for required libraries...")
    import ctypes.util

    if ctypes.util.find_library("aio") is None:
        msg = "libaio not found - required by MySQL %s" % (version, )
        if options.skip_libcheck:
            warn("    ! %s", msg)
            warn("    ! (continuing anyway due to --skip-libcheck")
        else:
            raise common.SandboxError(msg)

def distribution_from_download(options):
    """Deploy a MySQL distribution via a download from cdn.mysql.com

    Given a version specified by ``SandboxOptions.distribution`` attempt to
    find a binary tarball distribution from cdn.mysql.com and unpack the
    archive into the sandbox directory.

    This method will optionally cache downloads to avoid hitting the network
    for repeated sandbox deployments of the same version.  Downloads are
    cached in ~/.dbsake/cache and can be customized by setting the DBSAKE_CACHE
    environment variable to some other path.

    The tarball is verified by leveraging the etag provided by MySQL's
    CDN which provides an md5sum embedded in the etag.  This checksum is saved
    in the cache directory in a format understood by /usr/bin/md5sum so the
    download can be manually verified later.

    Other than the download logic, the resulting deployment is identical to the
    distribution_from_tarball method used if a binary distribution is provided
    directly to dbsake.

    :param options: SandboxOptions instance
    :raises: SandboxError on error
    """
    version = options.distribution # the --mysql-distribution option
    check_for_libaio(options)
    info("    - Attempting to deploy distribution for MySQL %s", version)
    checksum = hashlib.new('md5')
    with download_mysql(version, 'x86_64', options.cache_policy) as stream:
        managers = []
        stream.add(checksum.update)
        if os.isatty(sys.stderr.fileno()):
            stream_size = int(stream.info()['content-length'])
            stream.add(util.progressbar(max=stream_size))
        if options.cache_policy != 'never' and stream.headers['x-dbsake-cache']:
            managers.append(cache_download(stream.headers['x-dbsake-cache']))
            info("    - Caching download: %s", stream.headers['x-dbsake-cache'])
        else:
            debug("    # Not caching download")
        with contextlib.nested(*managers) as ctx:
            if ctx:
                stream.add(ctx[0].write)
                debug("Caching download to %s", ctx[0].name)
            info("    - Unpacking tar stream. This may take some time")
            unpack_tarball_distribution(stream, options.basedir)

    if checksum.hexdigest() != stream.headers['x-dbsake-checksum']:
        warn("    ! Detected checksum error in download")
        warn("    ! Expected MD5 checksum %s but computed %s",
             stream.headers['x-dbsake-checksum'], checksum.hexdigest())
    elif options.cache_policy != 'never' and stream.headers['x-dbsake-cache']:
        cache_path = stream.headers['x-dbsake-cache']
        md5_path = cache_path + '.md5'
        with open(md5_path, 'wb') as fileobj:
            print("# MD5 checksum of cache file", file=fileobj)
            print("# size: %s" % stream.info()['content-length'], file=fileobj)
            print("%s  %s" % (checksum.hexdigest(), cache_path),
                  file=fileobj)
        info("    - Stored MD5 checksum for download: %s", fileobj.name)

    bindir = os.path.join(options.basedir, 'bin')
    version = mysqld_version(os.path.join(bindir, 'mysqld'))

    info("    - Using mysqld (v%s): %s", version, os.path.join(bindir, 'mysqld'))
    info("    - Using mysqld_safe: %s", os.path.join(bindir, 'mysqld_safe'))
    info("    - Using mysql: %s", os.path.join(bindir, 'mysql'))
    info("    - Using share directory: %s", os.path.join(options.basedir, 'share'))
    info("    - Using mysqld --basedir: %s", options.basedir)
    plugin_dir = os.path.join(options.basedir, 'lib', 'plugin')
    if os.path.exists(plugin_dir):
        info("    - Using MySQL plugin directory: %s", os.path.join(options.basedir, 'lib', 'plugin'))

    return MySQLDistribution(
        version=version,
        mysqld=os.path.join(bindir, 'mysqld'),
        mysql=os.path.join(bindir, 'mysql'),
        mysqld_safe=os.path.join(bindir, 'mysqld_safe'),
        basedir=options.basedir,
        sharedir=os.path.join(options.basedir, 'share'),
        libexecdir=bindir,
        plugindir=plugin_dir,
    )
