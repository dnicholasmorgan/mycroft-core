# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import gc
import json
import sys
import time
from glob import glob
from itertools import chain

import os
from os.path import exists, join, basename, dirname, expanduser, isfile
from threading import Thread, Event

from msm import MycroftSkillsManager, SkillRepo, MsmException
from mycroft import dialog
from mycroft.enclosure.api import EnclosureAPI
from mycroft.configuration import Configuration
from mycroft.messagebus.message import Message
from mycroft.api import is_paired, DeviceApi
from mycroft.util import connected
from mycroft.util.log import LOG

from .core import load_skill, create_skill_descriptor, MainModule


DEBUG = Configuration.get().get("debug", False)
skills_config = Configuration.get().get("skills")
BLACKLISTED_SKILLS = skills_config.get("blacklisted_skills", [])
PRIORITY_SKILLS = skills_config.get("priority_skills", [])

installer_config = Configuration.get().get("SkillInstallerSkill")

MINUTES = 60  # number of seconds in a minute (syntatic sugar)


def _get_last_modified_date(path):
    """
        Get last modified date excluding compiled python files, hidden
        directories and the settings.json file.

        Args:
            path:   skill directory to check

        Returns:
            int: time of last change
    """
    all_files = []
    for root_dir, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if (not f.endswith('.pyc') and f != 'settings.json' and
                    not f.startswith('.')):
                all_files.append(join(root_dir, f))
    # check files of interest in the skill root directory
    return max(os.path.getmtime(f) for f in all_files)


class SkillManager(Thread):
    """ Load, update and manage instances of Skill on this system.

    Arguments:
        bus (eventemitter): Mycroft messagebus connection
    """

    def __init__(self, bus):
        super(SkillManager, self).__init__()
        self._stop_event = Event()
        self._connected_event = Event()

        self.loaded_skills = {}
        self.bus = bus
        self.enclosure = EnclosureAPI(bus)

        # Schedule install/update of default skill
        self.msm = self.create_msm()
        self.num_install_retries = 0

        self.update_interval = Configuration.get()['skills']['update_interval']
        self.update_interval = int(self.update_interval * 60 * MINUTES)
        self.dot_msm = join(self.msm.skills_dir, '.msm')
        if exists(self.dot_msm):
            self.next_download = os.path.getmtime(self.dot_msm) + \
                                 self.update_interval
        else:
            self.next_download = time.time() - 1

        # Conversation management
        bus.on('skill.converse.request', self.handle_converse_request)

        # Update on initial connection
        bus.on('mycroft.internet.connected',
               lambda x: self._connected_event.set())

        # Update upon request
        bus.on('skillmanager.update', self.schedule_now)
        bus.on('skillmanager.list', self.send_skill_list)
        bus.on('skillmanager.deactivate', self.deactivate_skill)
        bus.on('skillmanager.keep', self.deactivate_except)
        bus.on('skillmanager.activate', self.activate_skill)

    @staticmethod
    def create_msm():
        config = Configuration.get()
        msm_config = config['skills']['msm']
        repo_config = msm_config['repo']
        data_dir = expanduser(config['data_dir'])
        skills_dir = join(data_dir, msm_config['directory'])
        repo_cache = join(data_dir, repo_config['cache'])
        platform = config['enclosure'].get('platform', 'default')
        return MycroftSkillsManager(
            platform=platform, skills_dir=skills_dir,
            repo=SkillRepo(
                repo_cache, repo_config['url'], repo_config['branch']
            ), versioned=msm_config['versioned']
        )

    @staticmethod
    def load_skills_data() -> dict:
        """Contains info on how skills should be updated"""
        skills_data_file = expanduser('~/.mycroft/skills.json')
        if isfile(skills_data_file):
            with open(skills_data_file) as f:
                return json.load(f)
        else:
            return {}

    @staticmethod
    def write_skills_data(data: dict):
        skills_data_file = expanduser('~/.mycroft/skills.json')
        with open(skills_data_file, 'w') as f:
            json.dump(data, f)

        if (is_paired and
                Configuration.get()['skills'].get('upload_skill_manifest')):
            upload_data = SkillManager.convert_skills_data(data)
            try:
                DeviceApi().upload_skills_data(upload_data)
            except Exception as e:
                LOG.error('An error occured ({})'.format(e))

    @staticmethod
    def convert_skills_data(data: dict):
        """ Convert old style skill manifest to new style skill manifest. """
        msm = SkillManager.create_msm()
        defaults = [s.name for s in msm.list_defaults()]
        installed_skills = [s.name for s in msm.list() if s.is_local]
        ret = {'version': 1, 'blacklist': [], 'skills': []}
        for key in data:
            if (not isinstance(data[key], dict) or
                    key not in installed_skills):
                continue
            e = {}
            e['name'] = key
            e['beta'] = data[key].get('beta', False)
            e['installed'] = data[key].get('installed', 0)
            if isinstance(e['installed'], bool):
                e['installed'] = 0
            e['installation'] = data[key].get('installation', 'installed')
            e['status'] = data[key].get('status', 'active')
            e['failure-message'] = data[key].get('failure-message', '')
            e['updated'] = data[key].get('updated', 0)
            if 'origin' in data[key]:
                e['origin'] = data[key]['origin']
            else:
                if key in defaults:
                    e['origin'] = 'default'
                else:
                    e['origin'] = 'voice'

            ret['skills'].append(e)
        return ret

    def schedule_now(self, message=None):
        self.next_download = time.time() - 1

    @property
    def installed_skills_file(self):
        venv = dirname(dirname(sys.executable))
        if os.access(venv, os.W_OK | os.R_OK | os.X_OK):
            return join(venv, '.mycroft-skills')
        return expanduser('~/.mycroft/.mycroft-skills')

    def load_installed_skills(self) -> set:
        skills_file = self.installed_skills_file
        if not isfile(skills_file):
            return set()
        with open(skills_file) as f:
            return {
                i.strip() for i in f.read().split('\n') if i.strip()
            }

    def save_installed_skills(self, skill_names):
        with open(self.installed_skills_file, 'w') as f:
            f.write('\n'.join(skill_names))

    def download_skills(self, speak=False):
        """ Invoke MSM to install default skills and/or update installed skills

            Args:
                speak (bool, optional): Speak the result? Defaults to False
        """
        if not connected():
            LOG.error('msm failed, network connection not available')
            if speak:
                self.bus.emit(Message("speak", {
                    'utterance': dialog.get(
                        "not connected to the internet")}))
            self.next_download = time.time() + 5 * MINUTES
            return False

        installed_skills = self.load_installed_skills()
        default_groups = dict(self.msm.repo.get_default_skill_names())
        if self.msm.platform in default_groups:
            platform_groups = default_groups[self.msm.platform]
        else:
            LOG.info('Platform defaults not found, using DEFAULT skills only')
            platform_groups = []
        default_names = set(chain(default_groups['default'], platform_groups))
        default_skill_errored = False

        skills_data = self.load_skills_data()

        new_installs = []
        updated_skills = []

        def install_or_update(skill):
            """Install missing defaults and update existing skills"""
            if skills_data.get(skill.name, {}).get('beta'):
                skill.sha = None  # Will update to latest version
            if skill.is_local:
                skill.update()
                updated_skills.append(skill.name)
                if skill.name not in installed_skills:
                    skill.update_deps()
                    installed_skills.add(skill.name)
            elif skill.name in default_names:
                try:
                    new_installs.append(skill.name)
                    skill.install()
                except Exception:
                    if skill.name in default_names:
                        LOG.warning(
                            'Failed to install default skill: ' + skill.name
                        )
                        nonlocal default_skill_errored
                        default_skill_errored = True
                    raise
                installed_skills.add(skill.name)

        try:
            self.msm.apply(install_or_update, self.msm.list())
        except MsmException as e:
            LOG.error('Failed to update skills: {}'.format(repr(e)))

        for skill_name in new_installs + updated_skills:
            if skill_name not in skills_data or skill_name in new_installs:
                t = time.time() if skill_name in new_installs else 1
                skills_data.setdefault(skill_name, {})['installed'] = t
                skills_data[skill_name]['updated'] = 0

        self.write_skills_data(skills_data)
        self.save_installed_skills(installed_skills)

        if speak:
            data = {'utterance': dialog.get("skills updated")}
            self.bus.emit(Message("speak", data))

        if default_skill_errored and self.num_install_retries < 10:
            self.num_install_retries += 1
            self.next_download = time.time() + 5 * MINUTES
            return False
        self.num_install_retries = 0

        with open(self.dot_msm, 'a'):
            os.utime(self.dot_msm, None)
        self.next_download = time.time() + self.update_interval

        return True

    def _unload_removed(self, paths):
        """ Shutdown removed skills.

            Arguments:
                paths: list of current directories in the skills folder
        """
        paths = [p.rstrip('/') for p in paths]
        skills = self.loaded_skills
        # Find loaded skills that doesn't exist on disk
        removed_skills = [str(s) for s in skills.keys() if str(s) not in paths]
        for s in removed_skills:
            LOG.info('removing {}'.format(s))
            try:
                LOG.debug('Removing: {}'.format(skills[s]))
                skills[s]['instance'].default_shutdown()
            except Exception as e:
                LOG.exception(e)
            self.loaded_skills.pop(s)

    def _load_or_reload_skill(self, skill_path):
        """
            Check if unloaded skill or changed skill needs reloading
            and perform loading if necessary.

            Returns True if the skill was loaded/reloaded
        """
        skill_path = skill_path.rstrip('/')
        skill = self.loaded_skills.setdefault(skill_path, {})
        skill.update({
            "id": basename(skill_path),
            "path": skill_path
        })

        # check if folder is a skill (must have __init__.py)
        if not MainModule + ".py" in os.listdir(skill_path):
            return False

        # getting the newest modified date of skill
        modified = _get_last_modified_date(skill_path)
        last_mod = skill.get("last_modified", 0)

        # checking if skill is loaded and hasn't been modified on disk
        if skill.get("loaded") and modified <= last_mod:
            return False  # Nothing to do!

        # check if skill was modified
        elif skill.get("instance") and modified > last_mod:
            # check if skill has been blocked from reloading
            if (not skill["instance"].reload_skill or
                    not skill.get('active', True)):
                return False

            LOG.debug("Reloading Skill: " + basename(skill_path))
            # removing listeners and stopping threads
            try:
                skill["instance"].default_shutdown()
            except Exception:
                LOG.exception("An error occured while shutting down {}"
                              .format(skill["instance"].name))

            if DEBUG:
                gc.collect()  # Collect garbage to remove false references
                # Remove two local references that are known
                refs = sys.getrefcount(skill["instance"]) - 2
                if refs > 0:
                    msg = ("After shutdown of {} there are still "
                           "{} references remaining. The skill "
                           "won't be cleaned from memory.")
                    LOG.warning(msg.format(skill['instance'].name, refs))
            del skill["instance"]
            self.bus.emit(Message("mycroft.skills.shutdown",
                                  {"path": skill_path,
                                   "id": skill["id"]}))

        skill["loaded"] = True
        desc = create_skill_descriptor(skill_path)
        skill["instance"] = load_skill(desc,
                                       self.bus, skill["id"],
                                       BLACKLISTED_SKILLS)

        skill["last_modified"] = modified
        if skill['instance'] is not None:
            self.bus.emit(Message('mycroft.skills.loaded',
                                  {'path': skill_path,
                                   'id': skill['id'],
                                   'name': skill['instance'].name,
                                   'modified': modified}))
            return True
        else:
            self.bus.emit(Message('mycroft.skills.loading_failure',
                                  {'path': skill_path,
                                   'id': skill['id']}))
        return False

    def load_priority(self):
        skills = {skill.name: skill for skill in self.msm.list()}
        for skill_name in PRIORITY_SKILLS:
            skill = skills[skill_name]
            if not skill.is_local:
                try:
                    skill.install()
                except Exception:
                    LOG.exception('Downloading priority skill:' + skill.name)
                    if not skill.is_local:
                        continue
            self._load_or_reload_skill(skill.path)

    def remove_git_locks(self):
        """If git gets killed from an abrupt shutdown it leaves lock files"""
        for i in glob(join(self.msm.skills_dir, '*/.git/index.lock')):
            LOG.warning('Found and removed git lock file: ' + i)
            os.remove(i)

    def run(self):
        """ Load skills and update periodically from disk and internet """

        self.remove_git_locks()
        self._connected_event.wait()
        has_loaded = False

        # check if skill updates are enabled
        update = Configuration.get()["skills"]["auto_update"]

        # Scan the file folder that contains Skills.  If a Skill is updated,
        # unload the existing version from memory and reload from the disk.
        while not self._stop_event.is_set():
            # Update skills once an hour if update is enabled
            if time.time() >= self.next_download and update:
                self.download_skills()

            # Look for recently changed skill(s) needing a reload
            # checking skills dir and getting all skills there
            skill_paths = glob(join(self.msm.skills_dir, '*/'))
            still_loading = False
            for skill_path in skill_paths:
                still_loading = (
                        self._load_or_reload_skill(skill_path) or
                        still_loading
                )
            if not has_loaded and not still_loading and len(skill_paths) > 0:
                has_loaded = True
                self.bus.emit(Message('mycroft.skills.initialized'))

            self._unload_removed(skill_paths)
            # Pause briefly before beginning next scan
            time.sleep(2)

    def send_skill_list(self, message=None):
        """
            Send list of loaded skills.
        """
        try:
            info = {}
            for s in self.loaded_skills:
                is_active = (self.loaded_skills[s].get('active', True) and
                             self.loaded_skills[s].get('instance') is not None)
                info[basename(s)] = {
                    'active': is_active,
                    'id': self.loaded_skills[s]['id']
                }
            self.bus.emit(Message('mycroft.skills.list', data=info))
        except Exception as e:
            LOG.exception(e)

    def __deactivate_skill(self, skill):
        """ Deactivate a skill. """
        for s in self.loaded_skills:
            if skill in s:
                skill = s
                break
        try:
            self.loaded_skills[skill]['active'] = False
            self.loaded_skills[skill]['instance'].default_shutdown()
        except Exception as e:
            LOG.error('Couldn\'t deactivate skill, {}'.format(repr(e)))

    def deactivate_skill(self, message):
        """ Deactivate a skill. """
        try:
            skill = message.data['skill']
            if skill in [basename(s) for s in self.loaded_skills]:
                self.__deactivate_skill(skill)
        except Exception as e:
            LOG.error('Couldn\'t deactivate skill, {}'.format(repr(e)))

    def deactivate_except(self, message):
        """ Deactivate all skills except the provided. """
        try:
            skill_to_keep = message.data['skill']
            LOG.info('DEACTIVATING ALL SKILLS EXCEPT {}'.format(skill_to_keep))
            if skill_to_keep in [basename(i) for i in self.loaded_skills]:
                for skill in self.loaded_skills:
                    if basename(skill) != skill_to_keep:
                        self.__deactivate_skill(skill)
            else:
                LOG.info('Couldn\'t find skill')
        except Exception as e:
            LOG.error('Error during skill removal, {}'.format(repr(e)))

    def __activate_skill(self, skill):
        if not self.loaded_skills[skill].get('active', True):
            self.loaded_skills[skill]['loaded'] = False
            self.loaded_skills[skill]['active'] = True

    def activate_skill(self, message):
        """ Activate a deactivated skill. """
        try:
            skill = message.data['skill']
            if skill == 'all':
                for s in self.loaded_skills:
                    self.__activate_skill(s)
            else:
                for s in self.loaded_skills:
                    if skill in s:
                        skill = s
                        break
                self.__activate_skill(skill)
        except Exception as e:
            LOG.error('Couldn\'t activate skill, {}'.format(repr(e)))

    def stop(self):
        """ Tell the manager to shutdown """
        self._stop_event.set()

        # Do a clean shutdown of all skills
        for name, skill_info in self.loaded_skills.items():
            instance = skill_info.get('instance')
            if instance:
                try:
                    instance.default_shutdown()
                except Exception:
                    LOG.exception('Shutting down skill: ' + name)

    def handle_converse_request(self, message):
        """ Check if the targeted skill id can handle conversation

        If supported, the conversation is invoked.
        """

        skill_id = message.data["skill_id"]
        utterances = message.data["utterances"]
        lang = message.data["lang"]

        # loop trough skills list and call converse for skill with skill_id
        for skill in self.loaded_skills:
            if (self.loaded_skills[skill]["instance"] and
                    self.loaded_skills[skill]["id"] == skill_id):
                try:
                    instance = self.loaded_skills[skill]["instance"]
                except BaseException:
                    LOG.error("converse requested but skill not loaded")
                    self.bus.emit(message.reply("skill.converse.response", {
                        "skill_id": 0, "result": False}))
                    return
                try:
                    result = instance.converse(utterances, lang)
                    self.bus.emit(message.reply("skill.converse.response", {
                        "skill_id": skill_id, "result": result}))
                    return
                except BaseException:
                    LOG.exception(
                        "Error in converse method for skill " + str(skill_id))
        self.bus.emit(message.reply("skill.converse.response",
                                    {"skill_id": 0, "result": False}))
