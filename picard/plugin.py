# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2007 Lukáš Lalinský
# Copyright (C) 2014 Shadab Zafar
# Copyright (C) 2015 Laurent Monin
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

from collections import defaultdict
from functools import partial
import imp
import json
import os.path
import shutil
import tempfile
import traceback
import zipfile
import zipimport

from PyQt5 import QtCore

from picard import (
    VersionError,
    config,
    log,
    version_from_string,
    version_to_string,
)
from picard.const import (
    PLUGINS_API,
    USER_PLUGIN_DIR,
)
import picard.plugins


_suffixes = [s[0] for s in imp.get_suffixes()]
_package_entries = ["__init__.py", "__init__.pyc", "__init__.pyo"]
_extension_points = []
_PLUGIN_MODULE_PREFIX = "picard.plugins."
_PLUGIN_MODULE_PREFIX_LEN = len(_PLUGIN_MODULE_PREFIX)
_PLUGIN_PACKAGE_SUFFIX = ".picard"
_PLUGIN_PACKAGE_SUFFIX_LEN = len(_PLUGIN_PACKAGE_SUFFIX)
_FILEEXTS = ['.py', '.pyc', '.pyo', '.zip']
_UPDATE_SUFFIX = '.update'
_UPDATE_SUFFIX_LEN = len(_UPDATE_SUFFIX)


def is_update(path):
    return path.endswith(_UPDATE_SUFFIX)


def strip_update_suffix(path):
    if not is_update(path):
        return path
    return path[:-_UPDATE_SUFFIX_LEN]


def is_zip(path):
    return path.endswith('.zip')


def strip_zip_suffix(path):
    if not is_zip(path):
        return path
    return path[:-4]


def is_package(path):
    return path.endswith(_PLUGIN_PACKAGE_SUFFIX)


def strip_package_suffix(path):
    if not is_package(path):
        return path
    return path[:-_PLUGIN_PACKAGE_SUFFIX_LEN]


def is_zipped_package(path):
    return path.endswith(_PLUGIN_PACKAGE_SUFFIX + '.zip')


def _plugin_name_from_path(path):
    path = os.path.normpath(path)
    if is_zip(path):
        name = os.path.basename(strip_zip_suffix(path))
        if is_package(name):
            return strip_package_suffix(name)
        else:
            return name
    elif os.path.isdir(path):
        for entry in _package_entries:
            if os.path.isfile(os.path.join(path, entry)):
                return os.path.basename(path)
    else:
        file = os.path.basename(path)
        if file in _package_entries:
            return None
        name, ext = os.path.splitext(file)
        if ext in _suffixes:
            return name
        return None


def load_manifest(archive_path):
    archive = zipfile.ZipFile(archive_path)
    manifest_data = None
    with archive.open('MANIFEST.json') as f:
        manifest_data = json.loads(str(f.read().decode()))
    return manifest_data


def zip_import(path):
    if (not is_zip(path) or not os.path.isfile(path)):
        return (None, None, None)
    try:
        importer = zipimport.zipimporter(path)
        plugin_name = _plugin_name_from_path(path)
        manifest_data = None
        if is_zipped_package(path):
            try:
                manifest_data = load_manifest(path)
            except Exception:
                pass
        return (importer, plugin_name, manifest_data)
    except zipimport.ZipImportError:
        return (None, None, None)


def _unregister_module_extensions(module):
    for ep in _extension_points:
        ep.unregister_module(module)


class ExtensionPoint(object):

    def __init__(self, label=None):
        if label is None:
            import uuid
            label = uuid.uuid4()
        self.label = label
        self.__dict = defaultdict(list)
        _extension_points.append(self)

    def register(self, module, item):
        if module.startswith(_PLUGIN_MODULE_PREFIX):
            name = module[_PLUGIN_MODULE_PREFIX_LEN:]
            log.debug("ExtensionPoint: %s register <- plugin=%r item=%r" % (self.label, name, item))
        else:
            name = None
            # uncomment to debug internal extensions loaded at startup
            # print("ExtensionPoint: %s register <- item=%r" % (self.label, item))
        self.__dict[name].append(item)

    def unregister_module(self, name):
        try:
            del self.__dict[name]
        except KeyError:
            # NOTE: needed due to defaultdict behaviour:
            # >>> d = defaultdict(list)
            # >>> del d['a']
            # KeyError: 'a'
            # >>> d['a']
            # []
            # >>> del d['a']
            # >>> #^^ no exception, after first read
            pass

    def __iter__(self):
        enabled_plugins = config.setting["enabled_plugins"]
        for name in self.__dict:
            if name is None or name in enabled_plugins:
                for item in self.__dict[name]:
                    yield item


class PluginShared(object):

    def __init__(self):
        super().__init__()


class PluginWrapper(PluginShared):

    def __init__(self, module, plugindir, file=None, manifest_data=None):
        super().__init__()
        self.module = module
        self.compatible = False
        self.dir = plugindir
        self._file = file
        self.data = manifest_data or self.module.__dict__

    @property
    def name(self):
        try:
            return self.data['PLUGIN_NAME']
        except KeyError:
            return self.module_name

    @property
    def module_name(self):
        name = self.module.__name__
        if name.startswith(_PLUGIN_MODULE_PREFIX):
            name = name[_PLUGIN_MODULE_PREFIX_LEN:]
        return name

    @property
    def author(self):
        try:
            return self.data['PLUGIN_AUTHOR']
        except KeyError:
            return ""

    @property
    def description(self):
        try:
            return self.data['PLUGIN_DESCRIPTION']
        except KeyError:
            return ""

    @property
    def version(self):
        try:
            return self.data['PLUGIN_VERSION']
        except KeyError:
            return ""

    @property
    def api_versions(self):
        try:
            return self.data['PLUGIN_API_VERSIONS']
        except KeyError:
            return []

    @property
    def file(self):
        if not self._file:
            return self.module.__file__
        else:
            return self._file

    @property
    def license(self):
        try:
            return self.data['PLUGIN_LICENSE']
        except KeyError:
            return ""

    @property
    def license_url(self):
        try:
            return self.data['PLUGIN_LICENSE_URL']
        except KeyError:
            return ""

    @property
    def files_list(self):
        return self.file[len(self.dir)+1:]


class PluginData(PluginShared):

    """Used to store plugin data from JSON API"""

    def __init__(self, d, module_name):
        self.__dict__ = d
        super().__init__()
        self.module_name = module_name

    def __getattribute__(self, name):
        try:
            return super().__getattribute__(name)
        except AttributeError:
            log.debug('Attribute %r not found for plugin %r', name, self.module_name)
            return None

    @property
    def files_list(self):
        return ", ".join(self.files.keys())


class PluginManager(QtCore.QObject):

    plugin_installed = QtCore.pyqtSignal(PluginWrapper, bool)
    plugin_updated = QtCore.pyqtSignal(str, bool)
    plugin_removed = QtCore.pyqtSignal(str, bool)
    plugin_errored = QtCore.pyqtSignal(str, str, bool)

    def __init__(self):
        super().__init__()
        self.plugins = []
        self._available_plugins = None  # None=never loaded, [] = empty

    @property
    def available_plugins(self):
        return self._available_plugins

    def plugin_error(self, name, error, log_func=None):
        if log_func is None:
            log_func = log.error
        log_func(error)
        self.plugin_errored.emit(name, error, False)

    def load_plugindir(self, plugindir):
        plugindir = os.path.normpath(plugindir)
        if not os.path.isdir(plugindir):
            log.info("Plugin directory %r doesn't exist", plugindir)
            return
        # first, handle eventual plugin updates
        for updatepath in [os.path.join(plugindir, file) for file in
                           os.listdir(plugindir) if file.endswith(_UPDATE_SUFFIX)]:
            path = strip_update_suffix(updatepath)
            name = _plugin_name_from_path(path)
            if name:
                self._remove_plugin(name)
                os.rename(updatepath, path)
                log.debug('Updating plugin %r (%r))', name, path)
            else:
                log.error('Cannot get plugin name from %r', updatepath)
        # now load found plugins
        names = set()
        for path in [os.path.join(plugindir, file) for file in os.listdir(plugindir)]:
            name = _plugin_name_from_path(path)
            if name:
                names.add(name)
        log.debug("Looking for plugins in directory %r, %d names found",
                  plugindir,
                  len(names))
        for name in sorted(names):
            try:
                self.load_plugin(name, plugindir)
            except Exception as e:
                log.error('Unable to load plugin: %s.\nError occured: %s', name, e)

    def load_plugin(self, name, plugindir):
        module_file = None
        (importer, module_name, manifest_data) = zip_import(os.path.join(plugindir, name + '.zip'))
        if importer:
            name = module_name
            if not importer.find_module(name):
                error = _("Failed loading zipped plugin %r") % name
                self.plugin_error(name, error)
                return None
            module_pathname = importer.get_filename(name)
        else:
            try:
                info = imp.find_module(name, [plugindir])
                module_file = info[0]
                module_pathname = info[1]
            except ImportError:
                error = _("Failed loading plugin %r") % name
                self.plugin_error(name, error)
                return None

        plugin = None
        try:
            index = None
            for i, p in enumerate(self.plugins):
                if name == p.module_name:
                    log.warning("Module %r conflict: unregistering previously"
                                " loaded %r version %s from %r",
                                p.module_name,
                                p.name,
                                p.version,
                                p.file)
                    _unregister_module_extensions(name)
                    index = i
                    break
            if not importer:
                plugin_module = imp.load_module(_PLUGIN_MODULE_PREFIX + name, *info)
            else:
                plugin_module = importer.load_module(_PLUGIN_MODULE_PREFIX + name)
            plugin = PluginWrapper(plugin_module, plugindir,
                                   file=module_pathname, manifest_data=manifest_data)
            compatible_versions = self._compatible_api_versions(plugin.api_versions)
            if compatible_versions:
                log.debug("Loading plugin %r version %s, compatible with API: %s",
                          plugin.name,
                          plugin.version,
                          ", ".join([version_to_string(v, short=True) for v in
                                     sorted(compatible_versions)]))
                plugin.compatible = True
                setattr(picard.plugins, name, plugin_module)
                if index is not None:
                    self.plugins[index] = plugin
                else:
                    self.plugins.append(plugin)
            else:
                error = _("Plugin '%s' from '%s' is not compatible with this "
                          "version of Picard.") % (plugin.name, plugin.file)
                self.plugin_error(plugin.name, error, log_func=log.warning)
        except VersionError as e:
            error = _("Plugin %r has an invalid API version string : %s") % (name, e)
            self.plugin_error(name, error)
        except BaseException:
            error = _("Plugin %r : %s") % (name, traceback.format_exc())
            self.plugin_error(name, error)
        if module_file is not None:
            module_file.close()
        return plugin

    def _compatible_api_versions(self, api_versions):
        versions = [version_from_string(v) for v in list(api_versions)]
        return set(versions) & set(picard.api_versions_tuple)

    def _get_existing_paths(self, plugin_name, plugin_dir, fileexts):
        dirpath = os.path.join(plugin_dir, plugin_name)
        if not os.path.isdir(dirpath):
            dirpath = None
        filepaths = [os.path.join(plugin_dir, f)
                     for f in os.listdir(plugin_dir)
                     if f in [plugin_name + ext for ext in fileexts]
                     ]
        return (dirpath, filepaths)

    def _remove_plugin_files(self, plugin_name, plugin_dir, with_update=False):
        plugin_name = strip_zip_suffix(plugin_name)
        log.debug("Remove plugin files and dirs : %r", plugin_name)
        dirpath, filepaths = self._get_existing_paths(plugin_name, plugin_dir, _FILEEXTS)
        if dirpath:
            if os.path.islink(dirpath):
                log.debug("Removing symlink %r", dirpath)
                os.remove(dirpath)
            elif os.path.isdir(dirpath):
                log.debug("Removing directory %r", dirpath)
                shutil.rmtree(dirpath)
        if filepaths:
            for filepath in filepaths:
                log.debug("Removing file %r", filepath)
                os.remove(filepath)
                if with_update:
                    update = filepath + _UPDATE_SUFFIX
                    if os.path.isfile(update):
                        log.debug("Removing file %r", update)
                        os.remove(update)

    def _remove_plugin(self, plugin_name, with_update=False):
        self._remove_plugin_files(plugin_name, USER_PLUGIN_DIR, with_update)
        _unregister_module_extensions(plugin_name)
        self.plugins = [p for p in self.plugins if p.module_name != plugin_name]

    def remove_plugin(self, plugin_name, with_update=False):
        self._remove_plugin(plugin_name, with_update=with_update)
        self.plugin_removed.emit(plugin_name, False)

    def _install_plugin_zip(self, plugin_name, plugin_data, update=False):
        # zipped module from download
        zip_plugin = plugin_name + '.zip'
        dst = os.path.join(USER_PLUGIN_DIR, zip_plugin)
        if update:
            dst += _UPDATE_SUFFIX
            if os.path.isfile(dst):
                os.remove(dst)
        ziptmp = tempfile.NamedTemporaryFile(delete=False,
                                             dir=USER_PLUGIN_DIR).name
        try:
            with open(ziptmp, "wb") as zipfile:
                zipfile.write(plugin_data)
                zipfile.flush()
                os.fsync(zipfile.fileno())
            os.rename(ziptmp, dst)
            log.debug("Plugin saved to %r", dst)
        except BaseException:
            try:
                os.remove(ziptmp)
            except (IOError, OSError):
                pass
            raise

    def _install_plugin_file(self, path, update=False):
        dst = os.path.join(USER_PLUGIN_DIR, os.path.basename(path))
        if update:
            dst += _UPDATE_SUFFIX
            if os.path.isfile(dst):
                os.remove(dst)
        shutil.copy2(path, dst)

    def _install_plugin_dir(self, plugin_name, path, update=False):
        dst = os.path.join(USER_PLUGIN_DIR, plugin_name)
        if update:
            dst += _UPDATE_SUFFIX
            if os.path.isdir(dst):
                shutil.rmtree(dst)
        shutil.copytree(path, dst)

    def install_plugin(self, path, update=False, overwrite_confirm=None, plugin_name=None,
                       plugin_data=None):
        """
            path is either:
                1) /some/dir/name.py
                2) /some/dir/name (directory containing __init__.py)
                3) /some/dir/name.zip (containing either 1 or 2)

        """
        if not plugin_name:
            plugin_name = _plugin_name_from_path(path)
        if plugin_name:
            try:
                if plugin_data:
                    self._install_plugin_zip(plugin_name, plugin_data, update=update)
                elif os.path.isfile(path):
                    self._install_plugin_file(path, update=update)
                elif os.path.isdir(path):
                    self._install_plugin_dir(plugin_name, path, update=update)

                if not update:
                    try:
                        installed_plugin = self.load_plugin(plugin_name, USER_PLUGIN_DIR)
                    except Exception as e:
                        log.error('Unable to load plugin: %s.\nError occured: %s', plugin_name, e)
                        installed_plugin = None

                    if installed_plugin is not None:
                        self.plugin_installed.emit(installed_plugin, False)
                else:
                    self.plugin_updated.emit(plugin_name, False)
            except (OSError, IOError):
                log.warning("Unable to copy %s to plugin folder %s" % (path, USER_PLUGIN_DIR))

    def query_available_plugins(self, callback=None):
        self.tagger.webservice.get(
            PLUGINS_API['host'],
            PLUGINS_API['port'],
            PLUGINS_API['endpoint']['plugins'],
            partial(self._plugins_json_loaded, callback=callback),
            priority=True,
            important=True
        )

    def is_available(self, plugin_name):
        return any(p.module_name == plugin_name for p in self._available_plugins)

    def _plugins_json_loaded(self, response, reply, error, callback=None):
        if error:
            self.tagger.window.set_statusbar_message(
                N_("Error loading plugins list: %(error)s"),
                {'error': reply.errorString()},
                echo=log.error
            )
            self._available_plugins = []
        else:
            try:
                self._available_plugins = [PluginData(data, key) for key, data in
                                           response['plugins'].items()
                                           if self._compatible_api_versions(data['api_versions'])]
            except (AttributeError, KeyError, TypeError):
                self._available_plugins = []
        if callback:
            callback()

    def enabled(self, name):
        return True


class PluginPriority:

    """
    Define few priority values for plugin functions execution order
    Those with higher values are executed first
    Default priority is PluginPriority.NORMAL
    """
    HIGH = 100
    NORMAL = 0
    LOW = -100


class PluginFunctions:

    """
    Store ExtensionPoint in a defaultdict with priority as key
    run() method will execute entries with higher priority value first
    """

    def __init__(self, label=None):
        self.functions = defaultdict(lambda : ExtensionPoint(label=label))

    def register(self, module, item, priority=PluginPriority.NORMAL):
        self.functions[priority].register(module, item)

    def run(self, *args, **kwargs):
        """Execute registered functions with passed parameters honouring priority"""
        for priority, functions in sorted(self.functions.items(),
                                          key=lambda i: i[0],
                                          reverse=True):
            for function in functions:
                function(*args, **kwargs)
