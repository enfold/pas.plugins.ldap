# -*- coding: utf-8 -*-

from bda.cache import Memcached
from bda.cache import NullCache
from bda.cache.interfaces import ICacheManager
from dogpile.cache import make_region
from dogpile.cache.api import NO_VALUE
from dogpile.cache.proxy import ProxyBackend
from node.ext.ldap.interfaces import ICacheProviderFactory
from pas.plugins.ldap.interfaces import ICacheSettingsRecordProvider
from pas.plugins.ldap.interfaces import ILDAPPlugin
from pas.plugins.ldap.interfaces import IPluginCacheHandler
from pas.plugins.ldap.interfaces import VALUE_NOT_CACHED
from zope.component import adapter
from zope.component import queryUtility
from zope.globalrequest import getRequest
from zope.interface import implementer

from logging import getLogger
import threading
import time


logger = getLogger('pas.plugins.ldap.cache')
KEY_PREFIX = 'pas.plugins.ldap.rediscache:'
redis_cache = make_region(
    name='pas.plugins.ldap.rediscache',
    key_mangler=lambda key: KEY_PREFIX + key,
    )


class LoggingProxy(ProxyBackend):

    def get_serialized(self, key):
        value = self.proxied.get_serialized(key)
        result = "HIT"
        if value is NO_VALUE:
            result = "MISS"
        logger.debug("Cache {} for key {}".format(result, key))
        return value

    def get(self, key):
        return self.get_serialized(key)

    def set_serialized(self, key, value):
        logger.debug("Setting value for key {}".format(key))
        return self.proxied.set_serialized(key, value)

    def set(self, key, value):
        return self.set_serialized(key, value)


class PasLdapCache(object):

    def __init__(self, servers):
        self._servers = servers

    @property
    def servers(self):
        return self._servers

    def disconnect(self):
        pass

    def __repr__(self):
        return "<{0} {1}>".format(self.__class__.__name__, self.servers)


@implementer(ICacheManager)
class PasLdapRedisCache(PasLdapCache):

    def __init__(self, servers):
        super(PasLdapRedisCache, self).__init__(servers)
        self._client = self._configure(servers[0])

    def _configure(self, server_url, expiration_time=300):
        client = redis_cache.configure(
            'dogpile.cache.redis',
            replace_existing_backend=True,
            arguments={
                'url': server_url,
                'redis_expiration_time': expiration_time,
                'distributed_lock': True,
                'thread_local_lock': False,
            },
            wrap=[LoggingProxy],
        )
        return client

    # ICacheProvider interface
    def setTimeout(self, timeout=300):
        if self._client.actual_backend.redis_expiration_time != timeout:
            self._client = self._configure(self._servers[0], timeout)

    def getData(self, func, key, force_reload=False, args=[], kwargs={}):
        ret = self.get(key, force_reload=force_reload)
        if ret is None:
            ret = func(*args, **kwargs)
            self.set(key, ret)
        return ret

    def get(self, key, force_reload=False):
        if force_reload:
            self._client.delete(key)
            return None

        res = self._client.get(key)
        if res is NO_VALUE:
            return None

        return res

    def set(self, key, value):
        return self._client.set(key, value)

    def rem(self, key):
        """deprecated, use __delitem___"""
        self._client.delete(key)

    def __delitem__(self, key):
        self._client.delete(key)

class PasLdapMemcached(Memcached, PasLdapCache):

    _servers = None

    def __init__(self, servers):
        self._servers = servers
        super(PasLdapMemcached, self).__init__(servers)

    def disconnect(self):
        self._client.disconnect_all()


@implementer(ICacheProviderFactory)
class cacheProviderFactory(object):
    # cache provider factory for node.ext.ldap

    _thread_local = threading.local()

    @property
    def _key(self):
        return "_v_{0}_PasLdapCache".format(self.__class__.__name__)

    @property
    def servers(self):
        recordProvider = queryUtility(ICacheSettingsRecordProvider)
        if not recordProvider:
            return ""

        value = recordProvider().value or ""
        return value.split()

    @property
    def cache(self):
        servers = self.servers
        if not servers:
            return NullCache()

        key = self._key

        # thread safety for memcached connections
        cache_provider = getattr(self._thread_local, key, None)
        if cache_provider is None:
            # Redis cache is stored directly on the instance
            cache_provider = getattr(self, key, None)

        # if cache_provider is set and server config has not changed
        # return cache_provider
        if cache_provider and \
           frozenset(cache_provider.servers) == frozenset(servers):
            return cache_provider
        elif cache_provider:
            # server config has changed, close all connections
            cache_provider.disconnect()
            del cache_provider

        # establish new cache connection and store it
        svr = servers[0].lower()
        if svr.startswith('redis') or svr.startswith('unix'):
            cache_provider = PasLdapRedisCache(servers)
            setattr(self, key, cache_provider)
        else:
            cache_provider = PasLdapMemcached(servers)
            setattr(self._thread_local, key, cache_provider)

        return cache_provider

    def __call__(self):
        return self.cache


def get_plugin_cache(context):
    if not context.plugin_caching:
        # bypass for testing
        return NullPluginCache(context)
    plugin_cache = IPluginCacheHandler(context, None)
    if plugin_cache is not None:
        return plugin_cache
    return RequestPluginCache(context)


@implementer(IPluginCacheHandler)
class NullPluginCache(object):
    def __init__(self, context):
        self.context = context

    def get(self):
        return VALUE_NOT_CACHED

    def set(self, value):
        pass


@implementer(IPluginCacheHandler)
class RequestPluginCache(object):
    def __init__(self, context):
        self.context = context

    def _key(self):
        return "_v_ldap_ugm_{0}_".format(self.context.getId())

    def get(self):
        request = getRequest()
        rcachekey = self._key()
        if request and rcachekey in list(request.keys()):
            return request[rcachekey]
        return VALUE_NOT_CACHED

    def set(self, value):
        request = getRequest()
        if request is not None:
            rcachekey = self._key()
            request[rcachekey] = value

    def invalidate(self):
        request = getRequest()
        rcachekey = self._key()
        if request and rcachekey in list(request.keys()):
            del request[rcachekey]


VOLATILE_CACHE_MAXAGE = 10  # 10s default maxage on volatile


@adapter(ILDAPPlugin)
class VolatilePluginCache(RequestPluginCache):
    def get(self):
        try:
            cachetime, value = getattr(self.context, self._key())
        except AttributeError:
            return VALUE_NOT_CACHED
        if time.time() - cachetime > VOLATILE_CACHE_MAXAGE:
            return VALUE_NOT_CACHED
        return value

    def set(self, value):
        setattr(self.context, self._key(), (time.time(), value))

    def invalidate(self):
        try:
            delattr(self.context, self._key())
        except AttributeError:
            pass
