# -*- coding: utf-8 -*-

# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2013 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import contextlib
import functools
import logging
import sys
import time
import uuid

import six

from kazoo import exceptions as k_exceptions
from kazoo.handlers import threading as k_threading
from kazoo.protocol import states as k_states
from kazoo.recipe import watchers as k_watchers

from zake import fake_storage as fs
from zake import utils
from zake import version

LOG = logging.getLogger(__name__)

# We provide a basic txn support (not as functional as zookeeper) and this
# was added in 3.4.0 so we will say we are 3.4.0 compat (until proven
# differently).
SERVER_VERSION = (3, 4, 0)
_NO_ACL_MSG = "ACLs not currently supported"


class _PartialClient(object):
    def __init__(self, storage):
        self.storage = storage
        self.session_id = None

    def delete(self, path, version=-1, recursive=False):
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")
        data_watches = []
        child_watches = []
        path = utils.normpath(path)
        with self.storage.lock:
            if path not in self.storage:
                raise k_exceptions.NoNodeError("No path %s" % (path))
            path_version = self.storage[path]['version']
            if version != -1 and path_version != version:
                raise k_exceptions.BadVersionError("Version mismatch"
                                                   " (%s != %s)"
                                                   % (version, path_version))

            if recursive:
                paths = [path]
                children = self.storage.get_children(path, only_direct=False)
                for p in six.iterkeys(children):
                    paths.append(p)
            else:
                children = self.storage.get_children(path, only_direct=False)
                if children:
                    raise k_exceptions.NotEmptyError("Path %s is not-empty"
                                                     " (%s children exist)"
                                                     % (path, len(children)))
                paths = [path]
            paths = list(reversed(sorted(set(paths))))
            for p in paths:
                self.storage.pop(p)
            parents = []
            for p in paths:
                parents.extend(self.storage.get_parents(p))
            parents = list(reversed(sorted(set(parents))))
            for p in parents:
                event = k_states.WatchedEvent(
                    type=k_states.EventType.DELETED,
                    state=k_states.KeeperState.CONNECTED,
                    path=p)
                child_watches.append(([p], event))
            for p in paths:
                event = k_states.WatchedEvent(
                    type=k_states.EventType.DELETED,
                    state=k_states.KeeperState.CONNECTED,
                    path=p)
                data_watches.append(([p], event))
        return (True, data_watches, child_watches)

    def set(self, path, value, version=-1):
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")
        if not isinstance(value, six.binary_type):
            raise TypeError("value must be a byte string")
        if not isinstance(version, int):
            raise TypeError("version must be an int")
        path = utils.normpath(path)
        try:
            stat = self.storage.set(path, value, version=version)
        except KeyError:
            raise k_exceptions.NoNodeError("No path %s" % (path))
        data_watches = []
        child_watches = []
        event = k_states.WatchedEvent(type=k_states.EventType.CHANGED,
                                      state=k_states.KeeperState.CONNECTED,
                                      path=path)
        data_watches.append(([path], event))
        return (stat, data_watches, child_watches)

    def create(self, path, value=b"", acl=None,
               ephemeral=False, sequence=False, makepath=False):
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")
        if not isinstance(value, six.binary_type):
            raise TypeError("value must be a byte string")
        if acl:
            raise NotImplementedError(_NO_ACL_MSG)
        data_watches = []
        child_watches = []
        with self.storage.lock:
            path = utils.normpath(path)
            if makepath:
                for parent_path in utils.partition_path(path)[0:-1]:
                    if parent_path not in self.storage:
                        result = self.create(parent_path)
                        data_watches.extend(result[1])
                        child_watches.extend(result[2])
            created, parents, path = self.storage.create(
                path, value=value, sequence=sequence,
                ephemeral=ephemeral, session_id=self.session_id)
        if parents:
            event = k_states.WatchedEvent(type=k_states.EventType.CHILD,
                                          state=k_states.KeeperState.CONNECTED,
                                          path=path)
            child_watches.append((parents, event))
        if created:
            event = k_states.WatchedEvent(type=k_states.EventType.CREATED,
                                          state=k_states.KeeperState.CONNECTED,
                                          path=path)
            data_watches.append(([path], event))
        return (path, data_watches, child_watches)


class FakeClient(object):
    """A fake mostly functional/good enough kazoo compat. client

    It can have its underlying storage mocked out (as well as exposes the
    listeners that are currently active and the watches that are currently
    active) so that said functionality can be examined & introspected by
    testing frameworks (while in use and after the fact).
    """

    def __init__(self, handler=None, storage=None, server_version=None):
        self._listeners = set()
        self._child_watches = collections.defaultdict(list)
        self._data_watches = collections.defaultdict(list)
        if handler is None:
            self._handler = k_threading.SequentialThreadingHandler()
            self._own_handler = True
        else:
            self._handler = handler
            self._own_handler = False
        if storage is not None:
            self._storage = storage
            self._own_storage = False
        else:
            self._storage = fs.FakeStorage(self._handler)
            self._own_storage = True
        self._partial_client = _PartialClient(self._storage)
        self._open_close_lock = self._handler.rlock_object()
        self._child_watches_lock = self._handler.rlock_object()
        self._data_watches_lock = self._handler.rlock_object()
        self._listeners_lock = self._handler.rlock_object()
        self._connected = False
        if server_version is None:
            self._server_version = SERVER_VERSION
        else:
            self._server_version = tuple(server_version)
            if not len(self._server_version):
                raise ValueError("Non-empty server version expected")
        self.expired = False
        self.logger = LOG
        # Helper objects that makes these easier to create.
        self.ChildrenWatch = functools.partial(k_watchers.ChildrenWatch, self)
        self.DataWatch = functools.partial(k_watchers.DataWatch, self)

    @property
    def handler(self):
        return self._handler

    @property
    def storage(self):
        return self._storage

    def command(self, cmd=b'ruok'):
        self.verify()
        if cmd == 'ruok':
            return 'imok'
        if cmd == 'stat':
            server_version = ".".join([str(s) for s in self._server_version])
            return "\n".join(['Zake the fake version: %s' % (version.VERSION),
                              'Mimicked version: %s' % (server_version),
                              'Mode: standalone'])
        if cmd == "kill":
            self.stop()
        return ''

    def verify(self):
        if not self._connected:
            raise k_exceptions.ConnectionClosedError("Connection has been"
                                                     " closed")
        if self.expired:
            raise k_exceptions.SessionExpiredError("Expired")

    @property
    def session_id(self):
        return self._partial_client.session_id

    @property
    def timeout_exception(self):
        return IOError

    @property
    def child_watches(self):
        return self._child_watches

    @property
    def data_watches(self):
        return self._data_watches

    @property
    def listeners(self):
        return self._listeners

    @property
    def connected(self):
        return self._connected

    def sync(self, path):
        self.verify()
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")

    def server_version(self):
        self.verify()
        return self._server_version

    def flush(self):
        self.verify()

        # This puts an item into the callback queue, and waits until it gets
        # called, this is a cheap way of knowing that the queue has been
        # cycled over (as this item goes in on the bottom) and only when the
        # items ahead of this callback are finished will this get called.
        wait_for = self.handler.event_object()
        fired = False

        def flip():
            wait_for.set()

        while not wait_for.is_set():
            if not fired:
                self.handler.dispatch_callback(utils.make_cb(flip))
                fired = True
            time.sleep(0.001)

    def create(self, path, value=b"", acl=None,
               ephemeral=False, sequence=False, makepath=False):
        self.verify()
        result, data_watches, child_watches = self._partial_client.create(
            path, value=value, acl=acl, ephemeral=ephemeral, sequence=sequence,
            makepath=makepath)
        self.storage.inform(self, child_watches, data_watches)
        return result

    def create_async(self, path, value=b"", acl=None,
                     ephemeral=False, sequence=False, makepath=False):
        return self._generate_async(self.create, path, value=value,
                                    acl=acl, ephemeral=ephemeral,
                                    sequence=sequence, makepath=makepath)

    def get(self, path, watch=None):
        self.verify()
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")
        path = utils.normpath(path)
        try:
            (data, znode) = self.storage.get(path)
        except KeyError:
            raise k_exceptions.NoNodeError("No path %s" % (path))
        if watch:
            with self._data_watches_lock:
                self._data_watches[path].append(watch)
        return (data, znode)

    def set_acls(self, path, acls, version=-1):
        raise NotImplementedError(_NO_ACL_MSG)

    def set_acls_async(self, path, acls, version=-1):
        raise NotImplementedError(_NO_ACL_MSG)

    def get_acls_async(self, path):
        raise NotImplementedError(_NO_ACL_MSG)

    def get_acls(self, path):
        raise NotImplementedError(_NO_ACL_MSG)

    def get_async(self, path, watch=None):
        return self._generate_async(self.get, path, watch=watch)

    def start(self, timeout=None):
        with self._open_close_lock:
            if not self._connected:
                with self._child_watches_lock:
                    self._child_watches.clear()
                with self._data_watches_lock:
                    self._data_watches.clear()
                self._connected = True
                self.storage.attach(self)
                self.handler.start()
                self._partial_client.session_id = uuid.uuid4().int
                self._fire_state_change(k_states.KazooState.CONNECTED)

    def restart(self):
        with self._open_close_lock:
            before = self.session_id
            self.stop()
            self.start()
            return before

    def _fire_state_change(self, state):
        with self._listeners_lock:
            listeners = list(self._listeners)
        for func in listeners:
            self.handler.dispatch_callback(utils.make_cb(func, [state]))

    def _generate_async(self, func, *args, **kwargs):
        async_result = self.handler.async_result()

        def call(func, args, kwargs):
            try:
                result = func(*args, **kwargs)
                async_result.set(result)
                return result
            except Exception as exc:
                async_result.set_exception(exc)

        cb = utils.make_cb(call, [func, args, kwargs], type='async')
        self.handler.dispatch_callback(cb)
        return async_result

    def exists(self, path, watch=None):
        self.verify()
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")
        path = utils.normpath(path)
        try:
            (data, exists) = self.storage.get(path)
        except KeyError:
            exists = None
        if watch:
            with self._data_watches_lock:
                self._data_watches[path].append(watch)
        return exists

    def exists_async(self, path, watch=None):
        return self._generate_async(self.exists, path, watch=watch)

    def set(self, path, value, version=-1):
        self.verify()
        result, data_watches, child_watches = self._partial_client.set(
            path, value, version=version)
        self.storage.inform(self, child_watches, data_watches)
        return result

    def set_async(self, path, value, version=-1):
        return self._generate_async(self.set, path, value, version=version)

    def get_children(self, path, watch=None, include_data=False):
        self.verify()
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")

        def clean_path(p):
            return p.strip("/")

        path = utils.normpath(path)
        paths = self.storage.get_children(path)
        if watch:
            with self._child_watches_lock:
                self._child_watches[path].append(watch)
        if include_data:
            children_with_data = []
            for (p, data) in six.iteritems(paths):
                children_with_data.append(clean_path(p[len(path):]), data)
            return children_with_data
        else:
            children = []
            for p in list(six.iterkeys(paths)):
                children.append(clean_path(p[len(path):]))
            return children

    def get_children_async(self, path, watch=None, include_data=False):
        return self._generate_async(self.get_children, path,
                                    watch=watch, include_data=include_data)

    def stop(self):
        self.close()

    def delete(self, path, version=-1, recursive=False):
        self.verify()
        result, data_watches, child_watches = self._partial_client.delete(
            path, version=version, recursive=recursive)
        self.storage.inform(self, child_watches, data_watches)
        return result

    def delete_async(self, path, recursive=False):
        return self._generate_async(self.delete, path, recursive=recursive)

    def add_listener(self, listener):
        with self._listeners_lock:
            self._listeners.add(listener)

    def retry(self, func, *args, **kwargs):
        self.verify()
        return func(*args, **kwargs)

    def remove_listener(self, listener):
        with self._listeners_lock:
            self._listeners.discard(listener)

    def fire_child_watches(self, child_watches):
        if not self._connected:
            return
        for (paths, event) in child_watches:
            self._fire_watches(paths, event, self._child_watches,
                               self._child_watches_lock)

    def fire_data_watches(self, data_watches):
        if not self._connected:
            return
        for (paths, event) in data_watches:
            self._fire_watches(paths, event, self._data_watches,
                               self._data_watches_lock)

    def _fire_watches(self, paths, event, watch_source, watch_mutate_lock):
        dispatched = set()
        for path in reversed(sorted(paths)):
            if path in dispatched:
                continue
            with watch_mutate_lock:
                watches = list(watch_source.pop(path, []))
            for w in watches:
                self.handler.dispatch_callback(utils.make_cb(w, [event]))
            dispatched.add(path)

    def transaction(self):
        return FakeTransactionRequest(self)

    def ensure_path(self, path):
        self.verify()
        if not isinstance(path, six.string_types):
            raise TypeError("path must be a string")
        path = utils.normpath(path)
        for piece in utils.partition_path(path):
            try:
                self.create(piece)
            except k_exceptions.NodeExistsError:
                pass

    def ensure_path_async(self, path):
        return self._generate_async(self.ensure_path, path)

    def close(self, close_handler=True):
        with self._open_close_lock:
            if self._connected:
                self.storage.purge(self)
                self._fire_state_change(k_states.KazooState.LOST)
                if self._own_handler and close_handler:
                    self.handler.stop()
                self._connected = False
                self._partial_client.session_id = None


class StopTransaction(Exception):
    pass


class StopTransactionNoExists(StopTransaction):
    pass


class StopTransactionBadVersion(StopTransaction):
    pass


@contextlib.contextmanager
def try_txn_lock(lock):
    locked = lock.acquire(blocking=False)
    if not locked:
        raise RuntimeError("Transaction can not be concurrently modified")
    try:
        yield
    finally:
        lock.release()


class DelayedOperation(object):
    def __init__(self, name, operation, path=None, version=-1):
        self.path = path
        self.name = name
        self.version = version
        self._operation = operation

    def __call__(self):
        return self._operation()


class FakeTransactionRequest(object):
    def __init__(self, client):
        self._lock = client.handler.rlock_object()
        self._client = client
        self._partial_client = client._partial_client
        self._storage = client.storage
        self.operations = []
        self.committed = False

    @property
    def storage(self):
        return self._storage

    def delete(self, path, version=-1):
        delayed_op = functools.partial(self._partial_client.delete,
                                       path, version)
        self._add(DelayedOperation('delete', delayed_op,
                                   path=path, version=version))

    def check(self, path, version):

        def delayed_check(path, version):
            if not isinstance(path, six.string_types):
                raise TypeError("path must be a string")
            if not isinstance(version, int):
                raise TypeError("version must be an int")
            try:
                data = self._storage[path]
                if data['version'] != version:
                    raise StopTransactionBadVersion()
                else:
                    return (True, [], [])
            except KeyError:
                raise StopTransactionNoExists()

        delayed_op = functools.partial(delayed_check, path, version)
        self._add(DelayedOperation('check', delayed_op,
                                   path=path, version=version))

    def set_data(self, path, value, version=-1):
        delayed_op = functools.partial(self._partial_client.set,
                                       path, value, version)
        self._add(DelayedOperation('set_data', delayed_op,
                                   path=path, version=version))

    def create(self, path, value=b"", acl=None, ephemeral=False,
               sequence=False):
        delayed_op = functools.partial(self._partial_client.create,
                                       path, value, acl, ephemeral, sequence)
        self._add(DelayedOperation('create', delayed_op, path=path))

    def commit(self):
        self._check_tx_state()
        self._client.verify()
        with try_txn_lock(self._lock):
            self._check_tx_state()
            # Delay all watch firing until we are sure that it succeeded.
            results = []
            child_watches = []
            data_watches = []
            try:
                with self._storage.transaction():
                    for op in self.operations:
                        result = op()
                        results.append(result[0])
                        data_watches.extend(result[1])
                        child_watches.extend(result[2])
            except StopTransaction as e:
                for i in range(0, len(results)):
                    results[i] = k_exceptions.RolledBackError()
                if isinstance(e, StopTransactionBadVersion):
                    results.append(k_exceptions.BadVersionError())
                if isinstance(e, StopTransactionNoExists):
                    results.append(k_exceptions.NoNodeError())
                while len(results) != len(self.operations):
                    results.append(k_exceptions.RuntimeInconsistency())
            except (NotImplementedError, AttributeError,
                    RuntimeError, ValueError, TypeError,
                    k_exceptions.ConnectionClosedError,
                    k_exceptions.SessionExpiredError):
                # Allow all these errors to bubble up.
                six.reraise(*sys.exc_info())
            except Exception as e:
                for i in range(0, len(results)):
                    results[i] = k_exceptions.RolledBackError()
                results.append(e)
                while len(results) != len(self.operations):
                    results.append(k_exceptions.RuntimeInconsistency())
            else:
                self._storage.inform(self._client, child_watches, data_watches)
                self.committed = True
            return results

    def __enter__(self):
        return self

    def _check_tx_state(self):
        if self.committed:
            raise ValueError('Transaction already committed')

    def _add(self, request):
        with try_txn_lock(self._lock):
            self._check_tx_state()
            self.operations.append(request)

    def __exit__(self, type, value, tb):
        if not any((type, value, tb)):
            if not self.committed:
                self.commit()
