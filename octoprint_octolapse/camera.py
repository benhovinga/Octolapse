# coding=utf-8
##################################################################################
# Octolapse - A plugin for OctoPrint used for making stabilized timelapse videos.
# Copyright (C) 2019  Brad Hochgesang
##################################################################################
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see the following:
# https://github.com/FormerLurker/Octolapse/blob/master/LICENSE
#
# You can contact the author either through the git-hub repository, or at the
# following email address: FormerLurker@pm.me
##################################################################################
from __future__ import unicode_literals
import json
import six
import octoprint_octolapse.utility as utility
from threading import Thread
# This file is subject to the terms and conditions defined in
# file called 'LICENSE', which is part of this source code package.
import requests
import os
# Todo:  Do we need to add this to setup.py?
from requests.auth import HTTPBasicAuth
from requests.exceptions import SSLError
from octoprint_octolapse.settings import CameraProfile, MjpgStreamerControl, MjpgStreamer

# create the module level logger
from octoprint_octolapse.log import LoggingConfigurator
logging_configurator = LoggingConfigurator()
logger = logging_configurator.get_logger(__name__)


def format_request_template(camera_address, template, value):
    return template.format(camera_address=camera_address, value=value)


class CameraControl(object):

    camera_types = None
    camera_types_path = "webcam_types"
    camera_types_file = "camera_types.json"

    @staticmethod
    def _load_camera_types(data_path):
        # load all of the known camera types to allow custom control pages
        # custom pages aren't necessary, but they are much nicer to use
        # if they are available

        # we only need to load this once, so see if this is already complete
        if CameraControl.camera_types is not None:
            return
        camera_types = {
            "server_types": {}
        }
        # construct the path to the camera types file.  We'll reuse this later
        camera_type_path = os.path.join(data_path, "data", CameraControl.camera_types_path)
        # construct the file path to the camera types json file
        file_path = os.path.join(camera_type_path, CameraControl.camera_types_file)
        # load the list of available camera json files
        with open(file_path, 'r') as camera_files:
            camera_files = json.load(camera_files)

        server_types = camera_files["server_types"]
        # loop through each server type
        for key, server_type in six.iteritems(server_types):
            camera_types["server_types"][key] = {'cameras': {}}
            # load all of the individual camera type info files for the current server
            for file_name in server_type["file_names"]:
                # construct the file path
                file_path = os.path.join(camera_type_path, server_type["directory"], file_name)
                # load the file
                with open(file_path, 'r') as camera_info_file:
                    # extract the data from the json file
                    camera = json.load(camera_info_file)
                    # if the current server type is mjpgstreamer, convert the controls array to dict
                    # of MjpgStreamerControl objects
                    controls = {}
                    if key == MjpgStreamer.server_type:
                        for control in camera["controls"]:
                            control_setting = MjpgStreamerControl()
                            control_setting.update(control)
                            controls[control_setting.id] = control_setting
                        camera["controls"] = controls
                    # add the camera to our server type dictionary
                    camera_types["server_types"][key]["cameras"][camera["key"]] = camera

        CameraControl.camera_types = camera_types

    @staticmethod
    def get_webcam_type(data_path, server_type, data):
        # attempt to get the current camera type for the given server type and supplied data

        # first make sure our camera types are loaded
        # This will only happen once since the _load_camera_types function will return if it's already loaded
        CameraControl._load_camera_types(data_path)

        # return false if there is no data for the given server type
        if CameraControl.camera_types is None or server_type not in CameraControl.camera_types["server_types"]:
            return False

        # if the server type is mjpg-streamer, convert the data to a dict of
        if server_type == MjpgStreamer.server_type:
            controls = {}
            if isinstance(data, list):
                for control in data:
                    control_setting = MjpgStreamerControl()
                    control_setting.update(control)
                    controls[control_setting.id] = control_setting
            elif isinstance(data, dict):
                for key, control in six.iteritems(data):
                    control_setting = MjpgStreamerControl()
                    control_setting.update(control)
                    controls[key] = control_setting
            else:
                message = "The webcam preferences supplied by the server are of an unknown type: {0}".format(
                    type(data)
                )
                raise(CameraError("unknown-data-type", message))
            data = controls

        # get the available cameras for the given server type:
        cameras = CameraControl.camera_types["server_types"][server_type]["cameras"]

        # iterate the cameras dictionary for the given server type if possible
        for key, camera in six.iteritems(cameras):
            # see if the current camera type matches the given data
            if CameraControl._is_webcam_type(server_type, camera, data):
                return {
                    "key": camera["key"],
                    "name": camera["name"],
                    "make": camera["make"],
                    "model": camera["model"],
                    "template": camera["template"],
                }

        # no hits, the user will have to make due with the universal controls for now
        return False

    @staticmethod
    def _is_webcam_type(server_type, camera, data):
        if server_type == MjpgStreamer.server_type:
            # get the controls from the camera settings
            controls = camera["controls"]
            # create an MJPGStreamer settings object
            settings = MjpgStreamer()
            # update the settings from the camera data
            settings.update(camera)
            # check for a match
            if settings.controls_match_server(data):
                return True

        return False

    @staticmethod
    def apply_camera_settings(cameras, timeout_seconds=5):
        errors = []

        # make sure the supplied cameras are enabled and either webcams or script cameras
        cameras = [x for x in cameras if x.enabled and x.camera_type in [
            CameraProfile.camera_type_webcam,
            CameraProfile.camera_type_script
        ]]

        # create the threads
        threads = []
        for current_camera in cameras:
            thread = None
            if current_camera.camera_type == CameraProfile.camera_type_webcam:
                if current_camera.webcam_settings.server_type == MjpgStreamer.server_type:
                    thread = MjpgStreamerSettingsThread(profile=current_camera)
            elif current_camera.camera_type == CameraProfile.camera_type_script:
                thread = CameraSettingScriptThread(current_camera)

            if thread:
                threads.append(thread)

        # start the threads
        for thread in threads:
            thread.start()

        # join the threads
        for thread in threads:
            thread.join(timeout_seconds)
            if not thread.success:
                for error in thread.errors:
                    errors.append(error)

        return len(errors) == 0,  errors

    @staticmethod
    def apply_webcam_setting(
        server_type,
        setting,
        camera_name,
        address,
        username,
        password,
        ignore_ssl_error,
        timeout_seconds=5
    ):
        if server_type == 'mjpg-streamer':
            thread = MjpgStreamerSettingThread(
                setting,
                camera_name=camera_name,
                address=address,
                username=username,
                password=password,
                ignore_ssl_error=ignore_ssl_error,
                timeout_seconds=timeout_seconds
            )
            thread.start()
            thread.join()
            return thread.success, " - ".join([str(error) for error in thread.errors])
        else:
            message = "Cannot apply camera settings to the server type:{0}".format(server_type)
            logger.error(message)
            raise CameraError('server_type_not_found', message)

    @staticmethod
    def get_webcam_settings(
        server_type,
        camera_name,
        address,
        username,
        password,
        ignore_ssl_error,
        timeout_seconds=5
    ):
        errors = []
        controls = None

        if server_type == 'mjpg-streamer':
            thread = MjpgStreamerControlThread(
                address=address,
                username=username,
                password=password,
                ignore_ssl_error=ignore_ssl_error,
                camera_name=camera_name,
                timeout_seconds=timeout_seconds
            )
            thread.start()
            thread.join()
            if thread.errors:
                errors.append(" - ".join([str(error) for error in thread.errors]))

            if len(errors)>0:
                return len(errors) == 0, errors

            controls = thread.controls
            # clean up default values
            if not controls:
                raise CameraError(
                    "no-settings-found",
                    "No image preference controls could be returned from MjpgStreamer."
                )

            # turn this into a dictionary
            control_dict = {}
            for control in controls:
                control_dict[control.id] = control

            return len(errors) == 0, errors, {
                "webcam_settings": {
                    "mjpg_streamer": {
                        "controls": control_dict
                    }
                }
            }
        else:
            raise CameraError(
                "unknown-server",
                "The streaming server type does not currently support custom image preferences."
            )

    @staticmethod
    def get_webcam_server_type_from_request(r):
        webcam_server_type = "unknown"
        if "server" in r.headers:
            if r.headers["server"].startswith('MJPG-Streamer'):
                webcam_server_type = "mjpg-streamer"
            elif r.headers["server"].startswith('yawcam'):
                webcam_server_type = "yawcam"
            else:
                webcam_server_type = r.headers["server"]

        return webcam_server_type

    @staticmethod
    def test_mjpg_streamer_control(camera_profile, timeout_seconds=2):
        url = camera_profile.webcam_settings.address + "?action=command&id=-1"
        try:
            if len(camera_profile.webcam_settings.username) > 0:
                r = requests.get(url, auth=HTTPBasicAuth(camera_profile.webcam_settings.username,
                                                         camera_profile.webcam_settings.password),
                                 verify=not camera_profile.webcam_settings.ignore_ssl_error,
                                 timeout=float(timeout_seconds))
            else:
                r = requests.get(
                    url, verify=not camera_profile.webcam_settings.ignore_ssl_error, timeout=float(timeout_seconds))

            webcam_server_type = CameraControl.get_webcam_server_type_from_request(r)
            if webcam_server_type != "mjpg-streamer":
                message = "An unknown webcam server type '{0}' is being used for the '{1}' camera profile.  Currently " \
                          "only MJPEGStreamer is supported.  Unable to apply custom image preferences.".format(
                    webcam_server_type, camera_profile.name)
                logger.error(message)
                raise CameraError('unknown-server-type', message)
            if r.status_code == 501:
                message = "The server denied access to the mjpg-streamer control.htm for the '{0}' camera profile.  <a " \
                          "href=\"https://github.com/FormerLurker/Octolapse/wiki/Troubleshooting#why-cant-i-change" \
                          "-contrast-zoom-focus-etc\" target = \"_blank\">Please see this link to correct this " \
                          "error.</a>, or disable the 'Enable And Apply Preferences at Startup' and " \
                          "'Enable And Apply Preferences Before Print' options.".format(camera_profile.name)
                logger.error(message)
                raise CameraError("mjpg_streamer-control-error", message)
            if r.status_code != requests.codes.ok:
                message = "Status code received ({0}) was not OK.  Double check your webcam 'Base Addresss' address and " \
                          "your 'Snapshot Address Template'.  Or, disable the 'Enable And Apply Preferences at Startup' " \
                          "and 'Enable And Apply Preferences Before Print' options for the {1} camera " \
                          "profile and try again.".format(r.status_code, camera_profile.name)
                logger.error(message)
                raise CameraError('webcam_settings_apply_error', message)
        except SSLError as e:
            message = (
                "An SSL error was raised while testing custom image preferences for the '{0}' camera profile."
                    .format(camera_profile.name)
            )
            logger.error(message)
            raise CameraError('webcam_settings_ssl_error', message, cause=e)
        except CameraError as e:
            # Don't log exceptions that we raised.  They will be logged (hopefully) by the raiser.
            # re raise the error
            raise e
        except Exception as e:
            message = (
                "An unexpected error was raised while testing custom image preferences for the '{0}' camera profile."
                    .format(camera_profile.name)
            )
            logger.exception(message)
            raise CameraError('webcam_settings_apply_error', message, cause=e)

    @staticmethod
    def test_web_camera_image_preferences(camera_profile, timeout_seconds=2):
        assert (isinstance(camera_profile, CameraProfile))
        # first see what kind of server we have
        url = format_request_template(
            camera_profile.webcam_settings.address, camera_profile.webcam_settings.snapshot_request_template, "")
        try:
            if len(camera_profile.webcam_settings.username) > 0:
                r = requests.get(url, auth=HTTPBasicAuth(camera_profile.webcam_settings.username,
                                                         camera_profile.webcam_settings.password),
                                 verify=not camera_profile.webcam_settings.ignore_ssl_error,
                                 timeout=float(timeout_seconds))
            else:
                r = requests.get(url, verify=not camera_profile.webcam_settings.ignore_ssl_error,
                                 timeout=float(timeout_seconds))

            webcam_server_type = CameraControl.get_webcam_server_type_from_request(r)

        except SSLError as e:
            message = (
                "An SSL occurred while testing the '{0}' camera profile.  Look for the 'Camera Profile->Advanced Camera "
                "Opotions->Security->Ignore SSL Errors setting for a possible solution.".format(camera_profile.name)
            )
            logger.exception(message)
            raise CameraError('ssl-error', message, cause=e)
        except requests.ConnectionError as e:
            message = "Unable to connect to '{0}' for the '{1}' camera profile.  Please double check your 'Base Address' " \
                      "and 'Snapshot Address Template' settings.".format(url, camera_profile.name)
            logger.exception(message)
            raise CameraError('connection-error', message, cause=e)
        except Exception as e:
            message = "'An unexpected exception occured while testing the '{0}' camera profile.".format(
                camera_profile.name)
            logger.exception(message)
            raise CameraError('unknown-exception', message, cause=e)

        if webcam_server_type == "mjpg-streamer":
            CameraControl.test_mjpg_streamer_control(camera_profile, timeout_seconds)
        elif webcam_server_type == "yawcam":
            message = "You cannot use Yawcam with custom image preferences enabled.  Please disable custom image " \
                      "prefences for the  '{0}' camera profile. ".format(webcam_server_type, camera_profile.name)
            logger.error(message)
            raise CameraError('unsupported-server-type', message)
        else:
            message = "An unknown webcam server type '{0}' is being used for the '{1}' camera profile.  Currently only " \
                      "MJPEGStreamer is supported.  Unable to apply custom image preferences.".format(
                webcam_server_type,
                camera_profile.name)
            logger.error(message)
            raise CameraError('unknown-server-type', message)

    @staticmethod
    def test_web_camera(camera_profile, timeout_seconds=2, is_before_print_test=False):
        url = format_request_template(
            camera_profile.webcam_settings.address, camera_profile.webcam_settings.snapshot_request_template, "")
        try:
            if len(camera_profile.webcam_settings.username) > 0:
                r = requests.get(url, auth=HTTPBasicAuth(camera_profile.webcam_settings.username,
                                                         camera_profile.webcam_settings.password),
                                 verify=not camera_profile.webcam_settings.ignore_ssl_error,
                                 timeout=float(timeout_seconds))
            else:
                r = requests.get(
                    url, verify=not camera_profile.webcam_settings.ignore_ssl_error, timeout=float(timeout_seconds))
            if r.status_code == requests.codes.ok:
                if 'content-length' in r.headers and r.headers["content-length"] == 0:
                    message = "The request contained no data for the '{0}' camera profile.".format(camera_profile.name)
                    logger.error(message)
                    raise CameraError('request-contained-no-data', message)
                elif "image/jpeg" not in r.headers["content-type"].lower():
                    message = (
                        "The returned daata was not an image for the '{0}' camera profile.".format(camera_profile.name)
                    )
                    logger.error(message)
                    raise CameraError('not-an-image', message)
                elif not is_before_print_test or camera_profile.apply_settings_before_print:
                    CameraControl.test_web_camera_image_preferences(camera_profile, timeout_seconds)
            else:
                message = (
                    "An invalid status code or {0} was returned from the '{1}' camera profile."
                        .format(r.status_code, camera_profile.name)
                )
                logger.error(message)
                raise CameraError('invalid-status-code', message)

        except SSLError as e:
            message = "An SSL occurred while testing the '{0}' camera profile.".format(camera_profile.name)
            logger.exception(message)
            raise CameraError('ssl-error', message, cause=e)
        except requests.ConnectionError as e:
            message = (
                "Unable to connect to the provided snapshot URL of {0} for the '{1}' camera profile."
                .format(url, camera_profile.name)
            )
            logger.exception(message)
            raise CameraError('connection-error', message, cause=e)

        except requests.ConnectTimeout as e:
            message = "The connection to {0} timed out for the '{1}' camera profile.".format(url, camera_profile.name)
            logger.exception(message)
            raise CameraError('connection-timeout', message, cause=e)

        except requests.ReadTimeout as e:
            message = (
                "A read timeout occurred while connecting to {0} for the '{1}' camera profile."
                .format(url, camera_profile.name)
            )
            logger.exception(message)
            raise CameraError('read-timeout', message, cause=e)

        except requests.exceptions.InvalidSchema as e:
            message = (
                "An invalid schema was detected while connecting to {0} for the '{1}' camera profile."
                .format(url, camera_profile.name)
            )
            logger.exception(message)
            raise CameraError('invalid-schema', message, cause=e)
        except requests.exceptions.MissingSchema as e:
            message = (
                "A missing schema error was detected while connecting to {0} for the '{1}' camera profile."
                    .format(url, camera_profile.name)
            )
            logger.exception(message)
            raise CameraError('missing-schema', message, cause=e)

    @staticmethod
    def load_webcam_defaults(
        server_type,
        camera_name,
        address,
        username,
        password,
        ignore_ssl_error,
        timeout_seconds=5):

        if server_type == MjpgStreamer.server_type:

            # load the settings for the camera
            success, errors, settings = CameraControl.get_webcam_settings(
                server_type,
                camera_name,
                address,
                username,
                password,
                ignore_ssl_error,
                timeout_seconds=timeout_seconds
            )
            if not success:
                return False, errors
            # set the control values to the defaults
            controls = settings["webcam_settings"]["mjpg_streamer"]["controls"]
            for key, control in six.iteritems(controls):
                control.value = control.default
                controls[key] = control.to_dict()

            # apply the defaults
            thread = MjpgStreamerSettingsThread(
                controls=controls,
                address=address,
                username=username,
                password=password,
                ignore_ssl_error=ignore_ssl_error,
                camera_name=camera_name,
                timeout_seconds=timeout_seconds
            )
            thread.start()
            thread.join()

            return len(thread.errors) > 0, thread.errors, controls

        else:
            error = "Unable to load default settings for the server type {server_type}.  Currently only mjpg-streamer" \
                    " is supported. ".format(server_type)
            return False, [error], None


class MjpgStreamerThread(Thread):
    def __init__(
        self,
        profile=None,
        address=None,
        username=None,
        password=None,
        ignore_ssl_error=False,
        camera_name=None,
        timeout_seconds=4
    ):
        super(MjpgStreamerThread, self).__init__()
        if profile:
            self.address = profile.webcam_settings.address
            self.username = profile.webcam_settings.username
            self.password = profile.webcam_settings.password
            self.ignore_ssl_error = profile.webcam_settings.ignore_ssl_error
            self.timeout_seconds = timeout_seconds
            self.camera_name = profile.name
        else:
            self.address = address
            self.username = username
            self.password = password
            self.ignore_ssl_error = ignore_ssl_error
            self.timeout_seconds = timeout_seconds
            self.camera_name = camera_name

        self.errors = []
        self.success = False


class MjpgStreamerControlThread(MjpgStreamerThread):
    def __init__(
        self,
        profile=None,
        address=None,
        username=None,
        password=None,
        ignore_ssl_error=False,
        camera_name=None,
        timeout_seconds=4
    ):
        super(MjpgStreamerControlThread, self).__init__(
            profile=profile,
            address=address,
            username=username,
            password=password,
            ignore_ssl_error=ignore_ssl_error,
            camera_name=camera_name,
            timeout_seconds=timeout_seconds
        )
        self.controls = None

    def run(self):
        try:
            self.controls = self.get_controls_from_server()
        except CameraError as e:
            self.errors.append(e)

    def get_controls_from_server(self):
        input_file = self.get_file("input.json")
        control_settings = []
        if "controls" in input_file:
            # turn the control json into MjpgStreamerSetting
            for control in input_file["controls"]:
                control_setting = MjpgStreamerControl()
                control_setting.update(control)
                control_settings.append(control_setting)
        return control_settings

    def get_file(self, file_name):
        try:
            url = "{camera_address}{file_name}".format(camera_address=self.address, file_name=file_name)
            if len(self.username) > 0:
                r = requests.get(url, auth=HTTPBasicAuth(self.username, self.password),
                                 verify=not self.ignore_ssl_error, timeout=float(self.timeout_seconds))
            else:
                r = requests.get(url, verify=not self.ignore_ssl_error,
                                 timeout=float(self.timeout_seconds))

            if r.status_code == 501:
                message = "Access was denied to the mjpg-streamer {0} file for the " \
                          "'{1}' camera profile.".format(file_name, self.camera_name)
                logger.error(message)
                raise CameraError("mjpg_streamer-control-error", message)
            if r.status_code != requests.codes.ok:
                message = (
                    "Recived a status code of ({0}) while retrieving the {1} file from the {2} camera profile.  Double "
                    "check your 'Base Address' and 'Snapshot Address Template' within your camera profile settings."
                        .format(r.status_code, file_name, self.camera_name)
                )
                logger.error(message)
                raise CameraError('webcam_settings_apply_error', message)

            data = json.loads(r.text)
            return data
        except SSLError as e:
            message = (
                "An SSL error was raised while retrieving the {0} file for the '{1}' camera profile."
                    .format(file_name, self.camera_name)
            )
            logger.exception(message)
            raise CameraError('ssl_error', message, cause=e)
        except Exception as e:
            message = (
                "An unexpected error was raised while retrieving the {0} file for the '{1}' camera profile."
                    .format(file_name, self.camera_name)
            )
            logger.exception(message)
            raise CameraError('unexpected-error', message, cause=e)


class MjpgStreamerSettingsThread(MjpgStreamerThread):
    def __init__(
        self,
        profile=None,
        controls=None,
        address=None,
        username=None,
        password=None,
        ignore_ssl_error=False,
        camera_name=None,
        timeout_seconds=4
    ):
        super(MjpgStreamerSettingsThread, self).__init__(
            profile=profile,
            address=address,
            username=username,
            password=password,
            ignore_ssl_error=ignore_ssl_error,
            camera_name=camera_name,
            timeout_seconds=timeout_seconds
        )
        if profile:
            mjpg_streamer = profile.webcam_settings.mjpg_streamer.clone()
            self.controls = mjpg_streamer.controls
        else:
            self.controls = controls

    def run(self):
        try:
            self.apply_mjpg_streamer_settings()
        except CameraError as e:
            self.errors.append(e)

    def apply_mjpg_streamer_settings(self):
        threads = []
        has_started_thread = False
        logger.info("Applying all webcam image settings to the %s camera.", self.camera_name)
        # create the threads
        for key, control in six.iteritems(self.controls):
            thread = MjpgStreamerSettingThread(
                control,
                self.address,
                self.username,
                self.password,
                self.ignore_ssl_error,
                self.camera_name,
                timeout_seconds=self.timeout_seconds
            )
            threads.append(thread)

        if len(threads) > 0:
            logger.info("Applying all webcam image settings for the %s camera.", self.camera_name)
        for thread in threads:
            if not has_started_thread:
                has_started_thread = True
            thread.start()

        for thread in threads:
            thread.join()
            errors = thread.errors
            self.success = thread.success
            if not self.success:
                for error in errors:
                    self.errors.append(error)
        self.success = len(self.errors) > 0


class MjpgStreamerSettingThread(MjpgStreamerThread):
    def __init__(
        self,
        control,
        address,
        username,
        password,
        ignore_ssl_error,
        camera_name,
        timeout_seconds=4
     ):
        super(MjpgStreamerSettingThread, self).__init__(
            address=address,
            username=username,
            password=password,
            ignore_ssl_error=ignore_ssl_error,
            camera_name=camera_name,
            timeout_seconds=timeout_seconds
        )
        self.control = control

    def run(self):
        try:
            self.apply_mjpg_streamer_setting()
            self.success = len(self.errors) == 0
        except CameraError as e:
            self.errors.append(e)
        except Exception as e:
            self.errors.append(e)
            raise e

    def apply_mjpg_streamer_setting(self):

        if isinstance(self.control, dict):
            id = self.control["id"]
            group = self.control["group"]
            value = self.control["value"]
            name = self.control["name"]
        else:
            id = self.control.id
            group = self.control.group
            value = self.control.value
            name = self.control.name

        url = "{address}?action={action}&dest={dest}&plugin={plugin}&id={id}&group={group}&value={value}".format(
            address=self.address,
            action="command",
            dest=0,
            plugin=0,
            id=id,
            group=group,
            value=value
        )
        try:
            if len(self.username) > 0:
                r = requests.get(url, auth=HTTPBasicAuth(self.username, self.password),
                                 verify=not self.ignore_ssl_error, timeout=float(self.timeout_seconds))
            else:
                r = requests.get(url, verify=not self.ignore_ssl_error,
                                 timeout=float(self.timeout_seconds))

            webcam_server_type = CameraControl.get_webcam_server_type_from_request(r)
            if webcam_server_type != "mjpg-streamer":
                message = "An unknown webcam server '{0}' was detected while applying the {1} setting to the '{2}' camera " \
                          "profile.  Currently only MJPEGStreamer is supported.  Unable to apply custom image " \
                          "preferences. ".format(webcam_server_type, name, self.camera_name)
                logger.error(message)
                raise CameraError('unknown-server-type', message)
            if r.status_code == 501:
                message = "Access was denied to the mjpg-streamer control.html while applying the {0} setting to the '{" \
                          "1}' camera profile.  <a " \
                          "href=\"https://github.com/FormerLurker/Octolapse/wiki/Troubleshooting#why-cant-i-change" \
                          "-contrast-zoom-focus-etc\" target = \"_blank\">Please see this link to correct this " \
                          "error.</a>, disable the 'Enable And Apply Preferences at Startup' " \
                          "and 'Enable And Apply Preferences Before Print' options '.".format(name,
                                                                                              self.camera_name)
                logger.error(message)
                raise CameraError("mjpg_streamer-control-error", message)
            if r.status_code != requests.codes.ok:
                message = (
                    "Recived a status code of ({0}) while applying the {1} settings to the {2} camera profile.  "
                    "Double check your 'Base Address' and 'Snapshot Address Template' within your camera profile "
                    "settings.  Or disable 'Custom Image Preferences' for this profile and try again.".format(
                        r.status_code, name, self.camera_name
                    )
                )
                logger.error(message)
                raise CameraError('webcam_settings_apply_error', message)
        except CameraError as e:
            raise e
        except SSLError as e:
            message = (
                "An SSL error was raised while applying the {0} setting to the '{1}' camera profile."
                .format(name, self.camera_name)
            )
            logger.error(message)
            raise CameraError('ssl_error', message, cause=e)
        except Exception as e:
            message = (
                "An unexpected error was raised while applying the {0} setting to the '{1}' camera profile."
                .format(name, self.camera_name)
            )
            logger.error(message)
            raise CameraError('webcam_settings_apply_error', message, cause=e)


class CameraSettingScriptThread(Thread):
    def __init__(self, camera):
        super(CameraSettingScriptThread, self).__init__()
        self.Camera = camera
        self.camera_name = camera.name
        self.errors = []

    def run(self):
        try:
            script = self.Camera.on_print_start_script.strip()
            logger.info(
                "Executing the 'Before Print Starts' script at %s for the '%s' camera.", script, self.camera_name
            )
            if not script:
                message = "The Camera Initialization script is empty"
                logger.error(message)
                raise CameraError('no_camera_script_path', message)
            try:
                script_args = [
                    script,
                    self.Camera.name
                ]
                cmd = utility.POpenWithTimeout()
                return_code = cmd.run(script_args, None)
                console_output = cmd.stdout
                error_message = cmd.stderr
            except utility.POpenWithTimeout.ProcessError as e:
                message = "An OS Error error occurred while executing the custom camera initialization script"
                logger.exception(message)
                raise CameraError('camera_initialization_error', message, cause=e)

            if error_message is not None:
                if error_message.endswith("\r\n"):
                    error_message = error_message[:-2]
            if not return_code == 0:
                if error_message is not None:
                    error_message = "The custom camera initialization script failed with the following error message: {0}" \
                        .format(error_message)
                else:
                    error_message = (
                        "The custom camera initialization script returned {0},"
                        " which indicates an error.".format(return_code)
                    )
                logger.exception(error_message)
            elif error_message is not None:
                logger.warn(
                    "The console returned an error while running the script at %s for the '%s' camera.  Details:%s",
                    script, self.camera_name, error_message
                )
            else:
                logger.info(
                    "The 'Before Print Starts' script for the %s camera completed successfully.", self.camera_name
                )
                raise CameraError('camera_initialization_error', error_message)
        except CameraError as e:
            self.errors.append(e)


class CameraError(Exception):
    def __init__(self, error_type, message, cause=None):
        super(CameraError, self).__init__()
        self.error_type = error_type
        self.cause = cause if cause is not None else None
        self.message = message

    def __str__(self):
        if self.cause is None:
            return "{0}: {1}".format(self.error_type, self.message)
        return "{0}: {1} - Inner Exception: {2}".format(self.error_type, self.message, "{}".format(self.cause))
