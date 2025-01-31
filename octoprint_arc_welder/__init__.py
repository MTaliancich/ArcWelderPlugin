# coding=utf-8
# #################################################################################
# Arc Welder: Anti-Stutter
#
# A plugin for OctoPrint that converts G0/G1 commands into G2/G3 commands where possible and ensures that the tool
# paths don't deviate by more than a predefined resolution.  This compresses the gcode file sice, and reduces reduces
# the number of gcodes per second sent to a 3D printer that supports arc commands (G2 G3)
#
# Copyright (C) 2020  Brad Hochgesang
# #################################################################################
# This program is free software:
# you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see the following:
# https://github.com/MTaliancich/ArcWelderPlugin/blob/master/LICENSE
#
# You can contact the author either through the git-hub repository, or at the
# following email address: FormerLurker@pm.me
##################################################################################


import copy
import datetime
import logging
import os
import time
from shutil import copyfile

import octoprint.plugin
import tornado
from flask import request, jsonify
from octoprint.events import Events
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.storage import StorageError
from octoprint.server import util, app
from octoprint.server.util.flask import restricted_access
from octoprint.server.util.tornado import LargeResponseHandler
from packaging.version import Version
from six import string_types

import octoprint_arc_welder.firmware_checker as firmware_checker
import octoprint_arc_welder.log as log
import octoprint_arc_welder.preprocessor as preprocessor
import octoprint_arc_welder.utilities as utilities

MASTER = "master"

RC_DEVEL = "rc/devel"

RC_MAINTENANCE = "rc/maintenance"

# stupid python 2/python 3 compatibility imports

import queue

import urllib.parse as urllibparse

logging_configurator = log.LoggingConfigurator("arc_welder", "arc_welder.", "octoprint_arc_welder.")
root_logger = logging_configurator.get_root_logger()
# so that we can
logger = logging_configurator.get_logger("__init__")

# must import AFTER the logger is imported,
# else this will fail to log and may crash
import PyArcWelder as converter


class ArcWelderPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin
):
    if Version(octoprint.server.VERSION) >= Version("1.4"):
        import octoprint.access.permissions as permissions

        admin_permission = permissions.Permissions.ADMIN
    else:
        import flask_principal

        admin_permission = flask_principal.Permission(flask_principal.RoleNeed("admin"))

    PROCESS_OPTION_ALWAYS = "always"
    PROCESS_OPTION_UPLOAD_ONLY = "uploads-only"
    PROCESS_OPTION_SLICER_UPLOADS = "slicer-uploads"
    PROCESS_OPTION_GCODE_TRIGGER = "gcode-trigger"
    PROCESS_OPTION_MANUAL_ONLY = "manual-only"
    PROCESS_OPTION_DISABLED = "disabled"

    CHECK_FIRMWARE_ON_CONECT = "connection"
    CHECK_FIRMWARE_MANUAL_ONLY = "manual-only"
    CHECK_FIRMWARE_DISABLED = "disabled"
    SEND_WEBHOOK_DS = True
    # Gcode comment settings to control arcwelder
    # This is the tag that starts all settings.  Example: ; ArcWelder: {settings}
    ARC_WELDER_GCODE_TAG = "ARCWELDER"
    ARC_WELDER_GCODE_PARAMETERS = {
        "WELD": {"type": "boolean", "key": "weld"},
        "PRINT": {"type": "boolean", "key": "print"},
        "DELETE": {"type": "boolean", "key": "delete"},
        "SELECT": {"type": "boolean", "key": "select"},
        "PREFIX": {"type": "string", "key": "prefix"},
        "POSTFIX": {"type": "string", "key": "postfix"},
        "OVERWRITE-SOURCE": {"type": "boolean", "key": "overwrite-source"},
        "G90-INFLUENCES-EXTRUDER": {"type": "boolean", "key": "g90_g91_influences_extruder"},
        "RESOLUTION-MM": {"type": "float", "key": "resolution_mm"},
        "PATH-TOLERANCE-PERCENT": {"type": "percent", "key": "path_tolerance_percent"},
        "MAX-RADIUS-MM": {"type": "float", "key": "max_radius_mm"},
        "DYNAMIC-GCODE-PRECISION-ENABLED": {"type": "boolean", "key": "allow_dynamic_precision"},
        "DEFAULT-XYZ-PRECISION": {"type": "integer", "key": "default_xyz_precision"},
        "DEFAULT-E-PRECISION": {"type": "integer", "key": "default_e_precision"},
        "EXTRUSION-RATE-VARIANCE-DETECTION-ENABLED": {"type": "boolean",
                                                      "key": "extrusion_rate_variance_detection_enabled"},
        "EXTRUSION-RATE-VARIANCE-PERCENT": {"type": "percent", "key": "extrusion_rate_variance_percent"},
        "3D-ARCS-ENABLED": {"type": "boolean", "key": "allow_3d_arcs"},
        "TRAVEL-ARCS-ENABLED": {"type": "boolean", "key": "allow_travel_arcs"},
        "MAX-G2-G3-LENGTH-DETECTION-ENABLED": {"type": "boolean", "key": "max_gcode_length_detection_enabled"},
        "MAX-G2-G3-LENGTH": {"type": "integer", "key": "max_gcode_length"},
        "FIRMWARE-COMPENSATION-ENABLED": {"type": "boolean", "key": "firmware_compensation_enabled"},
        "MIN-ARC-SEGMENTS": {"type": "integer", "key": "min_arc_segments"},
        "MM-PER-ARC-SEGMENT": {"type": "float", "key": "mm_per_arc_segment"}
    }
    SEARCH_FUNCTION_SETTINGS = {
        "name": "settings",
        "type": utilities.COMMENT_SEARCH_TYPE_SETTINGS,
        "tag": ARC_WELDER_GCODE_TAG,
        "settings": ARC_WELDER_GCODE_PARAMETERS
    }
    SEARCH_FUNCTION_IS_WELDED = {
        "name": "is_welded",
        "type": utilities.COMMENT_SEARCH_TYPE_CONTAINS,
        "find": [
            "Postprocessed by [ArcWelder]".upper(),
            "Postprocessed by [ArcWelderLib]".upper(),
            "Postprocessed for the ArcWelderPlugin by [ArcWelderLib]".upper()
        ],
        "if_found": "return_this"
    }
    SEARCH_FUNCTION_CURA_UPLOAD = {
        "name": "slicer_upload_type",
        "type": utilities.COMMENT_SEARCH_TYPE_CONTAINS,
        "find": "Cura-OctoPrintPlugin".upper(),
        "value": "Cura-OctoPrintPlugin"
    }

    def __init__(self):
        super(ArcWelderPlugin, self).__init__()
        # get version information for PyArcWelder
        self.arc_welder_lib_version_info = converter.GetVersionInfo()
        self._logger = None
        # get version information from versioneer
        from ._version import get_versions
        self.source_version = get_versions()["version"]
        self.git_version = get_versions()["full-revisionid"]
        del get_versions

        # Note, you cannot count the number of items left to process using the
        # _processing_queue.  Items put into this queue will be inserted into
        # an internal dequeue by the preprocessor
        self._processing_queue = queue.Queue()
        self.settings_default = dict(
            use_octoprint_settings=True,
            g90_g91_influences_extruder=False,
            allow_3d_arcs=False,
            allow_travel_arcs=True,
            allow_dynamic_precision=False,
            default_xyz_precision=3,
            default_e_precision=5,
            max_gcode_length_detection_enabled=False,
            max_gcode_length=50,
            resolution_mm=0.05,
            path_tolerance_percent=5.0,  # 5%
            max_radius_mm=9999,  # 9.999 Meters
            extrusion_rate_variance_detection_enabled=True,
            extrusion_rate_variance_percent=5.0,
            firmware_compensation_enabled=False,
            min_arc_segments=14,  # 0 to disable
            mm_per_arc_segment=1.0,  # 0 to disable
            overwrite_source_file=False,
            target_prefix="",
            target_postfix=".aw",
            notification_settings=dict(
                show_queued_notification=True,
                show_started_notification=False,
                show_completed_notification=True
            ),
            feature_settings=dict(
                file_processing=ArcWelderPlugin.PROCESS_OPTION_ALWAYS,
                delete_source=ArcWelderPlugin.PROCESS_OPTION_DISABLED,
                select_after_processing=ArcWelderPlugin.PROCESS_OPTION_ALWAYS,
                print_after_processing=ArcWelderPlugin.PROCESS_OPTION_DISABLED,
                check_firmware=ArcWelderPlugin.CHECK_FIRMWARE_ON_CONECT,
                sendwebhook_after_processing=ArcWelderPlugin.SEND_WEBHOOK_DS,
                current_run_configuration_visible=True
            ),
            enabled=True,
            logging_configuration=dict(
                default_log_level=log.ERROR,
                log_to_console=False,
                enabled_loggers=[],
            ),
            version=self.source_version,
            git_version=self.git_version,
            arc_welder_lib_version_info=self.arc_welder_lib_version_info,
            discord=dict(
                bot_name="Octoprint",
                bot_avatar="",
                wh_url="",
                wh_thumbnails="",
                gcode_dir_path="",
                picture_dir_Path=""
            ),
            ftp=dict(
                host="",
                port="",
                user="",
                password="",
                gcode_dir_path="",
                picture_dir_Path=""
            )
        )
        # preprocessor worker
        self._preprocessor_worker = None
        # track removed files to handle moves
        self._recently_moved_files = []
        # a wait period for clearing recently moved files
        self._recently_moved_file_wait_ms = 1000

        # firmware checker
        self._firmware_checker = None

        # List of processes to cancel
        self._process_guids_to_cancel = []
        # flag to cancel all processing
        self._cancel_all_processing = False

    def get_settings_version(self):
        return 3

    def on_settings_migrate(self, target, current):
        # If we don't have a current version, look at the current settings file for the most recent version.
        if current is None:
            current = -1
        # Upgrade
        if target > current:
            logger.info("Upgrading settings.")
            if current < 2:
                logger.info("Migrating settings to version 2.")
                # change 'both' to 'always'
                if self._settings.get(["feature_settings", "file_processing"]) in ["both", "auto-only"]:
                    self._settings.set(["feature_settings", "file_processing"], ArcWelderPlugin.PROCESS_OPTION_ALWAYS)
                if self._settings.get(["feature_settings", "delete_source"]) in ["both", "auto-only"]:
                    self._settings.set(["feature_settings", "delete_source"], ArcWelderPlugin.PROCESS_OPTION_ALWAYS)
                if self._settings.get(["feature_settings", "select_after_processing"]) in ["both", "auto-only"]:
                    self._settings.set(["feature_settings", "select_after_processing"],
                                       ArcWelderPlugin.PROCESS_OPTION_ALWAYS)
                if self._settings.get(["feature_settings", "print_after_processing"]) in ["both", "auto-only"]:
                    self._settings.set(["feature_settings", "print_after_processing"],
                                       ArcWelderPlugin.PROCESS_OPTION_ALWAYS)
                if self._settings.get(["feature_settings", "sendwebhook_after_processing"]) in ["both", "auto-only"]:
                    self._settings.set(["feature_settings", "sendwebhook_after_processing"], True)
                logger.info("Settings migrated to version 2 successfully.")
            if current < 3:
                logger.info("Migrating settings to version 3.")
                extrusion_rate_variance = self._settings.get_float(["extrusion_rate_variance_percent"])
                self._settings.set(["extrusion_rate_variance_detection_enabled"], extrusion_rate_variance > 0)
                logger.info("Settings migrated to version 3 successfully.")
                max_gcode_length = self._settings.get_float(["max_gcode_length"])
                self._settings.set(["max_gcode_length_detection_enabled"], max_gcode_length > 0)
        elif current > target:
            # TODO:  Test downgrade
            logger.info("Downgrading settings.")
            if target < 3:
                logger.info("Downgrading settings to version 2.")
                extrusion_rate_variance_detection_enabled = self._settings.get_boolean(
                    ["extrusion_rate_variance_detection_enabled"])
                if not extrusion_rate_variance_detection_enabled:
                    self._settings.set(["extrusion_rate_variance_percent"], 0)

                max_gcode_length_detection_enabled = self._settings.get_boolean(["max_gcode_length_detection_enabled"])
                if not max_gcode_length_detection_enabled:
                    self._settings.set(["max_gcode_length"], 0)
                logger.info("Settings downgraded to version 2 successfully.")

    def on_after_startup(self):
        self._logger = logging.getLogger("octoprint.plugins.arc_welder")
        from octoprint.logging.handlers import CleaningTimedRotatingFileHandler
        hdlr = CleaningTimedRotatingFileHandler(
            self._settings.get_plugin_logfile_path(), when="D", backupCount=3)

        formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        hdlr.setFormatter(formatter)
        self._logger.addHandler(hdlr)

        # Initialise DiscordBot
        self._logger.info("arc_welder is started !")
        logging_configurator.configure_loggers(
            self._log_file_path, self._logging_configuration
        )
        self._preprocessor_worker = preprocessor.PreProcessorWorker(
            self.get_plugin_data_folder(),
            self._processing_queue,
            self._get_is_printing,
            self.preprocessing_started,
            self.preprocessing_progress,
            self.preprocessing_cancelled,
            self.preprocessing_failed,
            self.preprocessing_success,
            self.preprocessing_completed,
            self.preprocessing_cancellations
        )
        self._preprocessor_worker.daemon = True
        self._logger.info("Starting the Preprocessor worker thread.")
        self._preprocessor_worker.start()
        # start the firmware checker
        self._logger.info("Creating the firmware checker.")
        self._firmware_checker = firmware_checker.FirmwareChecker(
            self._plugin_version,
            self._printer,
            self._basefolder,
            self.get_plugin_data_folder(),
            self.check_firmware_response_received
        )
        if self._check_firmware_on_connect:
            if self._printer.is_operational():
                # The printer is connected, and we need to check the firmware
                self._logger.info("Checking Firmware Asynchronously.")
                self.check_firmware()
            else:
                self._logger.warning("The printer is not connected, cannot check firmware yet.")
        self._logger.info("Startup Complete.")

    # Events
    def get_settings_defaults(self):
        # plugin_version is not instantiated when __init__ is called.  Update it now.
        # self.settings_default["version"] = self._plugin_version
        return self.settings_default

    def on_settings_save(self, data):
        success, data, errors = self.check_settings(data)
        if not success:
            error_data = {
                "message_type": "setting-errors",
                "data": errors
            }
            self._plugin_manager.send_plugin_message(self._identifier, error_data)

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        # reconfigure logging
        logging_configurator.configure_loggers(
            self._log_file_path, self._logging_configuration
        )

    def check_settings(self, settings):
        # Check resolution, must be greater than 0
        errors = []
        key = ""
        try:
            key = "resolution_mm"
            if key in settings and float(settings[key]) <= 0:
                errors.append("The resolution must be greater than 0.")
                settings[key] = self._settings.get([key])

            key = "path_tolerance_percent"
            if key in settings and float(settings[key]) <= 0:
                errors.append("The path tolerance percent must be greater than 0%.")
                settings[key] = self._settings.get([key])
            key = "path_tolerance_percent"
            if key in settings and float(settings[key]) >= 100:
                errors.append("The path tolerance percent must be less than 100%.")
                settings[key] = self._settings.get([key])
            key = "mm_per_arc_segment"
            if key in settings and float(settings[key]) < 0:
                errors.append("MM Per Arc Segment (firmware compensation) must be greater than or equal to 0.")
                settings[key] = self._settings.get([key])
            key = "min_arc_segments"
            if key in settings and int(settings[key]) < 0:
                errors.append("Min Arc Segments (firmware compensation) must be greater than or equal to 0.")
                settings[key] = self._settings.get([key])

            key = "firmware_compensation_enabled"
            firmware_compensation_enabled = (
                bool(settings[key]) if key in settings
                else self._settings.get([key])
            )

            # checl firmware compensation variables.
            key = "mm_per_arc_segment"
            if key in settings and float(settings[key]) <= 0.0:
                if firmware_compensation_enabled:
                    # Only print this error if firmware compensation is enabled
                    errors.append("If firmware compensation is enabled, MM Per Arc Segment must be greater than 0.")
                settings[key] = self._settings.get([key])
            key = "min_arc_segments"
            if key in settings and int(settings[key]) <= 0.0:
                if firmware_compensation_enabled:
                    # Only print this error if firmware compensation is enabled
                    errors.append("If firmware compensation is enabled, Min Arc Segments must be greater than 0.")
                settings[key] = self._settings.get([key])

            key = "default_xyz_precision"
            if key in settings and (int(settings[key]) < 3 or int(settings[key]) > 6):
                errors.append("The XYZ Precision must be between 3 and 6")
                settings[key] = self._settings.get([key])

            key = "default_e_precision"
            if key in settings and (int(settings[key]) < 3 or int(settings[key]) > 6):
                errors.append("The E Precision must be between 3 and 6")
                settings[key] = self._settings.get([key])

            key = "extrusion_rate_variance_detection_enabled"
            extrusion_rate_variance_detection_enabled = (
                bool(settings[key]) if key in settings
                else self._settings.get([key])
            )
            key = "extrusion_rate_variance_percent"
            if key in settings and float(settings[key]) <= 0:
                if extrusion_rate_variance_detection_enabled:
                    errors.append("The Extrusion Rate Variance Percent must be greater than 0.")
                settings[key] = self._settings.get([key])

            key = "max_gcode_length_detection_enabled"
            max_gcode_length_detection_enabled = (
                bool(settings[key]) if key in settings
                else self._settings.get([key])
            )

            key = "max_gcode_length"
            if key in settings and int(settings[key]) < 31:
                if max_gcode_length_detection_enabled:
                    errors.append("The Max Gcode Length must be greater than 30.")
                settings[key] = self._settings.get([key])
            key = "max_radius_mm"
            if key in settings and float(settings[key]) <= 0:
                errors.append("The Maximum Arc Radius must be greater than 0.")
                settings[key] = self._settings.get([key])
        except TypeError as e:
            errors.append("An unexpected error occurred while saving your settings.  There was an error converting " +
                          key + ".  Please check the value and try again.")
            errors.append(e)

        return len(errors) == 0, settings, errors

    def get_template_configs(self):
        return [
            dict(
                type="settings",
                custom_bindings=True,
                template="arc_welder_settings.jinja2",
            )
        ]

    # Blueprints
    @octoprint.plugin.BlueprintPlugin.route("/cancelPreprocessing", methods=["POST"])
    @restricted_access
    def cancel_preprocessing_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            if not self._preprocessor_worker:
                return jsonify({"success": False,
                                "error": "The processor worker does not exist.  Try again later or restart Octoprint."})
            request_values = request.get_json()
            cancel_all = request_values.get("cancel_all", False)
            job_guid = request_values.get("guid", "")
            if cancel_all:
                self._logger.info("Cancelling all processing tasks.")
                self._cancel_all_processing = True
            else:
                self._logger.info("Cancelling job with guid %s.", job_guid)
                self._process_guids_to_cancel.append(job_guid)
            return jsonify({"success": True})

    @octoprint.plugin.BlueprintPlugin.route("/clearLog", methods=["POST"])
    @restricted_access
    def clear_log_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            request_values = request.get_json()
            clear_all = request_values["clear_all"]
            if clear_all:
                self._logger.info("Clearing all log files.")
            else:
                self._logger.info("Rolling over most recent log.")

            logging_configurator.do_rollover(clear_all=clear_all)
            return jsonify({"success": True})

    # Preprocess from file sidebar
    @octoprint.plugin.BlueprintPlugin.route("/process", methods=["POST"])
    @restricted_access
    def process_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            if not self._enabled:
                return jsonify({"success": False, "message": "Arc Welder is Disabled."})
            try:
                request_values = request.get_json()
                path = urllibparse.unquote(request_values["path"])
                origin = request_values["origin"]
                # I think this is the easiest way to get the name.

                metadata = self._file_manager.get_metadata(FileDestinations.LOCAL, path)
                if metadata is not None and "display" in metadata:
                    name = metadata["display"]
                else:
                    # probably don't need this, but better safe than sorry
                    path_part, name = self._file_manager.split_path(FileDestinations.LOCAL, path)

                # add the file and metadata to the processor queue
                success = self.add_file_to_preprocessor_queue(name, path, origin, True)
            except Exception as e:
                self._logger.exception("Could not process file manually.")
                raise e
            return jsonify({"success": success})

    @octoprint.plugin.BlueprintPlugin.route("/restoreDefaultSettings", methods=["POST"])
    @restricted_access
    def restore_default_settings_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            self._settings.set([], self.settings_default)
            # force save the settings and trigger a SettingsUpdated event
            self._settings.save(force=True, trigger_event=True)
            return jsonify({"success": True})

    @octoprint.plugin.BlueprintPlugin.route("/checkFirmware", methods=["POST"])
    @restricted_access
    def check_firmware_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            self._logger.debug("Manual firmware request received.")
            return jsonify({"success": self.check_firmware()})

    @octoprint.plugin.BlueprintPlugin.route("/getFirmwareVersion", methods=["POST"])
    @restricted_access
    def get_firmware_version_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            response = {
                "firmware_info": None,
                "firmware_types_info": None,
                "success": False,
            }
            if self._firmware_checker:
                firmware_types_info = self._firmware_checker.firmware_types_info
                if firmware_types_info and "last_checked_date" in firmware_types_info:
                    firmware_types_info["last_checked_date"] = (
                        utilities.to_local_date_time_string(firmware_types_info["last_checked_date"])
                    )
                response["firmware_info"] = self._firmware_checker.current_firmware_info
                response["firmware_types_info"] = firmware_types_info
                response["success"] = True
            return jsonify(response)

    @octoprint.plugin.BlueprintPlugin.route("/getPreprocessingTasks", methods=["POST"])
    @restricted_access
    def get_preprocessing_tasks_request(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            self.send_preprocessing_tasks_update()
            return jsonify({"success": True})

    @octoprint.plugin.BlueprintPlugin.route("/checkForFirmwareInfoUpdates", methods=["POST"])
    @restricted_access
    def check_for_firmware_info_update(self):
        with ArcWelderPlugin.admin_permission.require(http_exception=403):
            result = {
                "success": False,
                "new_version": None,
                "firmware_info": None,
                "firmware_types_info": None,
                "error": None
            }
            if self._firmware_checker:
                update_results = self._firmware_checker.check_for_updates()
                current_firmware_info = self._firmware_checker.current_firmware_info
                firmware_types_info = self._firmware_checker.firmware_types_info
                if firmware_types_info and "last_checked_date" in firmware_types_info:
                    firmware_types_info["last_checked_date"] = (
                        utilities.to_local_date_time_string(firmware_types_info["last_checked_date"])
                    )
                result["firmware_types_info"] = firmware_types_info
                result["firmware_info"] = current_firmware_info
                if update_results["success"]:
                    result["new_version"] = update_results["new_version"]
                    result["success"] = True
                else:
                    result["error"] = update_results["error"]
            return jsonify(result)

    # Callback Handler for /downloadFile
    # uses the ArcWelderLargeResponseHandler
    def download_file_request(self, request_handler):
        # get the args
        full_path = None
        file_type = request_handler.get_query_arguments('type')[0]
        if file_type == 'log':
            full_path = self._log_file_path
        if full_path is None or not os.path.isfile(full_path):
            raise tornado.web.HTTPError(404)
        return full_path

    def send_preprocessing_tasks_update(self):
        preprocessing_tasks = []
        if self._preprocessor_worker:
            preprocessing_tasks = self._preprocessor_worker.get_tasks()

        data = {
            "message_type": "preprocessing-tasks-changed",
            "preprocessing_tasks": preprocessing_tasks
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)

    def send_notification_toast(
            self, toast_type, title, message, auto_hide, key=None, close_keys=[]):
        data = {
            "message_type": "toast",
            "toast_type": toast_type,
            "title": title,
            "message": message,
            "auto_hide": auto_hide,
            "key": key,
            "close_keys": close_keys,
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)

    def check_firmware_response_received(self, result):
        if not result["success"]:
            self.send_notification_toast(
                toast_type="error",
                title="Unable To Check Firmware",
                message=result["error"],
                auto_hide=False,
                key="check-firmware",
                close_keys=["check-firmware"]
            )
        else:
            firmware_types_info = self._firmware_checker.firmware_types_info
            last_checked_date = firmware_types_info.get("last_checked_date", None)
            if last_checked_date:
                firmware_types_info["last_checked_date"] = utilities.to_local_date_time_string(last_checked_date)
            self.send_firmware_info_updated_message(
                result["firmware_version"], firmware_types_info
            )

    def send_firmware_info_updated_message(self, firmware_info, firmware_types_info):
        # convert the last_check_datetime to a local timezone
        if firmware_info and "last_check_datetime" in firmware_info:
            firmware_info["last_check_datetime"] = (
                utilities.to_local_date_time_string(firmware_info["last_check_datetime"])
            )
        data = {
            "message_type": "firmware-info-update",
            "firmware_info": firmware_info,
            "firmware_types_info": firmware_types_info,
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)

    def check_firmware(self):
        if self._firmware_checker is None or not self._enabled:
            if self._enabled:
                self._logger.error("The firmware checker is None, cannot check firmware!")
            return False
        if not self._printer.is_operational():
            self._logger.warning("Cannot check firmware, printer not operational.")
            return False
        self._logger.info("Checking Firmware Capabilities")
        return self._firmware_checker.check_firmware_async()

    # ~~ AssetPlugin mixin
    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return dict(
            js=[
                "js/showdown.min.js",
                "js/pnotify_extensions.js",
                "js/markdown_help.js",
                "js/arc_welder.js",
                "js/arc_welder.settings.js",
            ],
            css=["css/arc_welder.css"],
        )

    def _select_file(self, path):
        if path and len(path) > 1 and path[0] == '/':
            path = path[1:]
        self._printer.select_file(path, False)

    def _get_is_file_selected(self, path, origin):
        current_job = self._printer.get_current_job()
        current_file = current_job.get("file", {'path': "", "origin": ""})
        current_file_path = current_file["path"]
        # ensure the current file path starts with a /
        if current_file_path and current_file_path[0] != '/':
            current_file_path = '/' + current_file_path
        current_file_origin = current_file["origin"]
        return path == current_file_path and origin == current_file_origin

    def _get_is_printing(self, path=None):
        # If the printer is NOT printing, always return false
        if not self._printer.is_printing():
            return False
        # If the path parameter is provided, check for a locally printing file of the same path
        if path:
            return self._get_is_file_selected(path, FileDestinations.LOCAL)
        return True

    # Properties
    @property
    def _log_file_path(self):
        return self._settings.get_plugin_logfile_path()

    # Settings Properties
    @property
    def _logging_configuration(self):
        logging_configurator_ = self._settings.get(["logging_configuration"])
        if logging_configurator_ is None:
            logging_configurator_ = self.settings_default["logging_configuration"]
        return logging_configurator_

    @property
    def _gcode_conversion_log_level(self):
        if "enabled_loggers" not in self._logging_configuration:
            return log.ERROR
        else:
            enabled_loggers = self._logging_configuration["enabled_loggers"]
        log_level = log.CRITICAL
        for logger_ in enabled_loggers:
            if logger_["name"] == "arc_welder.gcode_conversion":
                log_level = logger_["log_level"]
                break
        return log_level

    @property
    def _enabled(self):
        enabled = self._settings.get_boolean(["enabled"])
        if enabled is None:
            enabled = self.settings_default["enabled"]
        return enabled

    @property
    def _use_octoprint_settings(self):
        use_octoprint_settings = self._settings.get_boolean(["use_octoprint_settings"])
        if use_octoprint_settings is None:
            use_octoprint_settings = self.settings_default["use_octoprint_settings"]
        return use_octoprint_settings

    @property
    def _g90_g91_influences_extruder(self):
        if self._use_octoprint_settings:
            g90_g91_influences_extruder = self._settings.global_get(
                ["feature", "g90InfluencesExtruder"]
            )
        else:
            g90_g91_influences_extruder = self._settings.get_boolean(
                ["g90_g91_influences_extruder"]
            )
        if g90_g91_influences_extruder is None:
            g90_g91_influences_extruder = self.settings_default[
                "g90_g91_influences_extruder"
            ]
        return g90_g91_influences_extruder

    @property
    def _allow_3d_arcs(self):
        return self._settings.get_boolean(["allow_3d_arcs"])

    @property
    def _allow_travel_arcs(self):
        return self._settings.get_boolean(["allow_travel_arcs"])

    @property
    def _allow_dynamic_precision(self):
        return self._settings.get_boolean(['allow_dynamic_precision'])

    @property
    def _default_xyz_precision(self):
        return self._settings.get_float(["default_xyz_precision"])

    @property
    def _default_e_precision(self):
        return self._settings.get_float(["default_e_precision"])

    @property
    def _max_gcode_length_detection_enabled(self):
        return self._settings.get_boolean(["max_gcode_length_detection_enabled"])

    @property
    def _max_gcode_length(self):
        if not self._max_gcode_length_detection_enabled:
            return 0
        return self._settings.get_float(["max_gcode_length"])

    @property
    def _resolution_mm(self):
        resolution_mm = self._settings.get_float(["resolution_mm"])
        if resolution_mm is None:
            resolution_mm = self.settings_default["resolution_mm"]
        return resolution_mm

    @property
    def _path_tolerance_percent(self):
        path_tolerance_percent = self._settings.get_float(["path_tolerance_percent"])
        if path_tolerance_percent is None:
            path_tolerance_percent = self.settings_default["path_tolerance_percent"]
        return path_tolerance_percent

    @property
    def _path_tolerance_percent_decimal(self):
        return self._path_tolerance_percent / 100.0

    @property
    def _extrusion_rate_variance_detection_enabled(self):
        return self._settings.get_boolean(["extrusion_rate_variance_detection_enabled"])

    @property
    def _extrusion_rate_variance_percent(self):
        if not self._extrusion_rate_variance_detection_enabled:
            # returning 0 disables extrusion rate variance detection
            return 0.0
        extrusion_rate_variance_percent = self._settings.get_float(["extrusion_rate_variance_percent"])
        if extrusion_rate_variance_percent is None or extrusion_rate_variance_percent < 0:
            extrusion_rate_variance_percent = self.settings_default["extrusion_rate_variance_percent"]
        return extrusion_rate_variance_percent

    @property
    def _max_radius_mm(self):
        max_radius_mm = self._settings.get_float(["max_radius_mm"])
        if max_radius_mm is None:
            max_radius_mm = self.settings_default["max_radius_mm"]
        return max_radius_mm

    @property
    def _firmware_compensation_enabled(self):
        firmware_compensation_enabled = self._settings.get_boolean(["firmware_compensation_enabled"])
        if firmware_compensation_enabled is None:
            firmware_compensation_enabled = False
        return firmware_compensation_enabled

    @property
    def _min_arc_segments(self):
        if not self._firmware_compensation_enabled:
            # returning 0 disabled firmware compensation
            return 0.0
        min_arc_segments = self._settings.get_float(["min_arc_segments"])
        if min_arc_segments is None or min_arc_segments < 0:
            min_arc_segments = self.settings_default["min_arc_segments"]
        return min_arc_segments

    @property
    def _mm_per_arc_segment(self):
        if not self._firmware_compensation_enabled:
            # returning 0 disabled firmware compensation
            return 0.0
        mm_per_arc_segment = self._settings.get_float(["mm_per_arc_segment"])
        if mm_per_arc_segment is None or mm_per_arc_segment < 0:
            mm_per_arc_segment = self.settings_default["mm_per_arc_segment"]
        return mm_per_arc_segment

    @property
    def _overwrite_source_file(self):
        overwrite_source_file = self._settings.get_boolean(["overwrite_source_file"])
        return overwrite_source_file or (
                self._target_prefix == "" and self._target_postfix == ""
        )

    @property
    def _target_prefix(self):
        target_prefix = self._settings.get(["target_prefix"])
        if target_prefix is None:
            target_prefix = self.settings_default["target_prefix"]
        else:
            target_prefix = target_prefix.strip()
        return target_prefix

    @property
    def _target_postfix(self):
        target_postfix = self._settings.get(["target_postfix"])
        if target_postfix is None:
            target_postfix = self.settings_default["target_postfix"]
        else:
            target_postfix = target_postfix.strip()
        return target_postfix

    @property
    def _check_firmware_on_connect(self):
        return self._settings.get(["feature_settings", "check_firmware"]) == ArcWelderPlugin.CHECK_FIRMWARE_ON_CONECT

    @property
    def _show_queued_notification(self):
        return self._settings.get(["notification_settings", "show_queued_notification"])

    @property
    def _show_started_notification(self):
        return self._settings.get(["notification_settings", "show_started_notification"])

    @property
    def _show_completed_notification(self):
        return self._settings.get(["notification_settings", "show_completed_notification"])

    @property
    def _file_processing(self):
        return self._settings.get(["feature_settings", "file_processing"])

    @property
    def _delete_source(self):
        return self._settings.get(["feature_settings", "delete_source"])

    @property
    def _select_after_processing(self):
        return self._settings.get(["feature_settings", "select_after_processing"])

    @property
    def _print_after_processing(self):
        return self._settings.get(["feature_settings", "print_after_processing"])

    @property
    def _sendwebhook_after_processing(self):
        return self._settings.get(["feature_settings", "sendwebhook_after_processing"])

    @property
    def _discord_botName(self):
        return self._settings.get(["discord", "bot_name"])

    @property
    def _discord_botAvatar(self):
        return self._settings.get(["discord", "bot_avatar"])

    @property
    def _discord_whUrl(self):
        return self._settings.get(["discord", "wh_url"])

    @property
    def _discord_whThumbnails(self):
        return self._settings.get(["discord", "wh_thumbnails"])

    @property
    def _discord_picturePath(self):
        return self._settings.get(["discord", "picture_dir_path"])

    @property
    def _discord_gcodePath(self):
        return self._settings.get(["discord", "gcode_dir_path"])

    @property
    def _ftp_host(self):
        return self._settings.get(["ftp", "host"])

    @property
    def _ftp_user(self):
        return self._settings.get(["ftp", "user"])

    @property
    def _ftp_password(self):
        return self._settings.get(["ftp", "password"])

    @property
    def _ftp_picturePath(self):
        return self._settings.get(["ftp", "picture_dir_path"])

    @property
    def _ftp_gcodePath(self):
        return self._settings.get(["ftp", "gcode_dir_path"])

    def _get_process_file(self, is_manual):
        if is_manual:
            # always process if manually triggered.
            return True
        setting = self._file_processing
        if setting == ArcWelderPlugin.PROCESS_OPTION_ALWAYS:
            # Always process if the 'always' option is selected
            return True
        # Can't do this yet, maybe later
        # if setting == ArcWelderPlugin.PROCESS_OPTION_SLICER_UPLOADS and is_slicer_upload:
        #    # process slicer uploads if the slicer option is selected
        #    return True
        return False

    def _get_delete_source(self):
        setting = self._delete_source
        return setting == ArcWelderPlugin.PROCESS_OPTION_ALWAYS and not self._overwrite_source_file

    def _get_select_after_processing(self, is_upload):
        setting = self._select_after_processing
        if setting == ArcWelderPlugin.PROCESS_OPTION_ALWAYS:
            return True
        if setting == ArcWelderPlugin.PROCESS_OPTION_UPLOAD_ONLY and is_upload:
            return True
        return False

    def _get_print_after_processing(self, is_manual):
        setting = self._print_after_processing
        if setting == ArcWelderPlugin.PROCESS_OPTION_ALWAYS:
            return True
        if is_manual and setting == ArcWelderPlugin.PROCESS_OPTION_MANUAL_ONLY:
            return True
        # Reserve for a future version
        # if is_slicer_upload and setting == ArcWelderPlugin.PROCESS_OPTION_SLICER_UPLOADS:
        #    return True
        return False

    def _get_sendwebhook_after_processing(self):
        return self._sendwebhook_after_processing

    def upload_file(self, gcode_file_name):
        import ftplib
        try:
            self._logger.debug("\n\nupload_file()")
            self._logger.debug(
                "upload_file host: %s, port: %s, password: %s" % (self._ftp_host, self._ftp_user, self._ftp_password))
            session = ftplib.FTP(self._ftp_host, self._ftp_user, self._ftp_password)
            self._logger.debug("upload_file plugin_data_folder:")
            self._logger.debug(self.get_plugin_data_folder())
            picture_file_name = gcode_file_name.replace('gcode', 'png')
            data_folder = os.path.join(os.getcwd(), self.get_plugin_data_folder(), "..")
            gcode_folder = os.path.join(data_folder, '..', 'uploads')
            picture_folder = os.path.join(data_folder, 'UltimakerFormatPackage')
            gcode_file_path = os.path.join(gcode_folder, gcode_file_name)
            picture_file_path = os.path.join(picture_folder, picture_file_name)

            gcode_file = open(gcode_file_path, 'rb')  # file to send
            picture_file = open(picture_file_path, 'rb')
            session.storbinary("STOR /www/%s/%s" % (self._ftp_gcodePath, gcode_file_name), gcode_file)  # send the file
            session.storbinary("STOR /www/%s/%s" % (self._ftp_picturePath, picture_file_name),
                               picture_file)  # send the file
            self._logger.info("upload_file uploaded: %s, %s" % (gcode_file_name, picture_file_name))
            gcode_file.close()  # close file and FTP
            picture_file.close()
            session.quit()
            self._logger.info("upload_file send wh: %s, %s" % (gcode_file_name, picture_file_name))
            self.sendwhdiscord(gcode_file_name, picture_file_name)
        except Exception as e:
            self._logger.error("upload_file error %s" % e)

    def get_output_file_name_and_path(self, display_name, storage_path, gcode_comment_settings):

        path, name = self._file_manager.split_path(FileDestinations.LOCAL, storage_path)
        new_display_name = name
        new_name = name
        # see what the plugin settings say about overwriting the target
        overwrite_source_file = self._overwrite_source_file
        if not overwrite_source_file:
            target_prefix = self._target_prefix
            target_prefix = gcode_comment_settings.get("prefix", target_prefix)
            target_postfix = self._target_postfix
            target_postfix = gcode_comment_settings.get("postfix", target_postfix)
            if target_prefix == "" and target_postfix == "":
                # our gcode comment settings say we should overwrite the source, so do it!
                overwrite_source_file = True
            else:
                # generate a new file name
                file_name = utilities.remove_extension_from_filename(name)
                new_display_name = utilities.remove_extension_from_filename(display_name)
                file_extension = utilities.get_extension_from_filename(name)
                display_extension = utilities.get_extension_from_filename(display_name)
                new_name = "{0}{1}{2}.{3}".format(target_prefix, file_name, target_postfix, file_extension)
                new_display_name = "{0}{1}{2}.{3}".format(target_prefix, new_display_name, target_postfix,
                                                          display_extension)
        if overwrite_source_file:
            new_name = name
            new_display_name = display_name

        new_path = self._file_manager.join_path(FileDestinations.LOCAL, path, new_name)

        return new_name, new_path, new_display_name

    def save_preprocessed_file(self, task, results, additional_metadata):
        # get the file name and path
        processor_args = task["processor_args"]
        octoprint_args = task["octoprint_args"]
        source_name = octoprint_args["source_name"]
        source_path = octoprint_args["source_path"]
        target_path = octoprint_args["target_path"]
        target_name = octoprint_args["target_name"]
        target_display_name = octoprint_args["target_display_name"]

        if self._get_is_printing(target_path):
            raise TargetFileSaveError(
                "The source file will be overwritten, but it is currently printing, cannot overwrite.")

        if source_path == target_path:
            self._logger.info("Overwriting source file '%s' with the processed file '%s'.", source_name, target_name)
            # first remove the source file
            try:
                self._remove_file_from_filemanager(source_path)
            except StorageError:
                self._logger.error(
                    "Unable to overwrite the target file, it is currently in use.  Writing to new file."
                )
                # get a collision free filename and save it like that
                target_directory, target_name, target_display_name = self._get_collision_free_filepath(target_path,
                                                                                                       target_display_name)
                target_path = self._file_manager.join_path(FileDestinations.LOCAL, target_directory, target_name)
                self._logger.info("Saving target to new collision free path at %s", target_name)
                task["octoprint_args"]["cant_overwrite"] = True
                task["octoprint_args"]["target_path"] = target_path
                task["octoprint_args"]["target_name"] = target_name
                task["octoprint_args"]["target_display_name"] = target_display_name
        else:
            self._logger.info("Arc compression complete, creating a new gcode file: %s", target_name)

        new_file_object = octoprint.filemanager.util.DiskFileWrapper(
            target_name, processor_args["target_path"], move=True
        )

        self._file_manager.add_file(
            FileDestinations.LOCAL,
            target_path,
            new_file_object,
            allow_overwrite=True,
            display=target_display_name,
        )
        self._file_manager.set_additional_metadata(
            FileDestinations.LOCAL, target_path, "arc_welder", True, overwrite=True, merge=False
        )
        progress = results["progress"]
        # copy the progress so we don't change the original
        metadata = copy.copy(progress)
        # add source and target name
        metadata["source_name"] = source_name
        metadata["target_name"] = target_name
        metadata["target_display_name"] = target_display_name

        self._file_manager.set_additional_metadata(
            FileDestinations.LOCAL,
            target_path,
            "arc_welder_statistics",
            metadata,
            overwrite=True,
            merge=False
        )

        # Add compatibility for ultimaker thumbnail package
        has_ultimaker_format_package_thumbnail = (
                "thumbnail" in additional_metadata
                and isinstance(additional_metadata['thumbnail'], string_types)
                and additional_metadata['thumbnail'].startswith('plugin/UltimakerFormatPackage/thumbnail/')
        )
        # Add compatibility for PrusaSlicer thumbnail package
        has_prusa_slicer_thumbnail = (
                "thumbnail" in additional_metadata
                and isinstance(additional_metadata['thumbnail'], string_types)
                and additional_metadata['thumbnail'].startswith('plugin/prusaslicerthumbnails/thumbnail/')
        )

        # delete the thumbnail src element if it exists, we will add it later if necessary
        if "thumbnail_src" in additional_metadata:
            del additional_metadata["thumbnail_src"]

        if has_ultimaker_format_package_thumbnail and "thumbnail_src" not in additional_metadata:
            additional_metadata["thumbnail_src"] = "UltimakerFormatPackage"
        elif has_prusa_slicer_thumbnail and "thumbnail_src" not in additional_metadata:
            additional_metadata["thumbnail_src"] = "prusaslicerthumbnails"

        # add the additional metadata
        if "thumbnail" in additional_metadata:
            current_path = additional_metadata["thumbnail"]
            thumbnail_src = None
            thumbnail = None
            if has_ultimaker_format_package_thumbnail:
                thumbnail_src = "UltimakerFormatPackage"
            elif has_prusa_slicer_thumbnail:
                thumbnail_src = "prusaslicerthumbnails"

            if thumbnail_src is not None:
                thumbnail = self.copy_thumbnail(thumbnail_src, current_path, target_name)
            # set the thumbnail path and src.  It will not be copied to the final metadata if the value is none
            additional_metadata["thumbnail"] = thumbnail
            additional_metadata["thumbnail_src"] = thumbnail_src

        # add all the metadata items
        for key, value in list(additional_metadata.items()):
            if value is not None:
                self._file_manager.set_additional_metadata(
                    FileDestinations.LOCAL,
                    target_path,
                    key,
                    value,
                    overwrite=True,
                    merge=False
                )

        return metadata

    def _remove_file_from_filemanager(self, path):

        num_tries = 0
        seconds_to_wait = 2
        max_tries = 5
        while True:
            try:
                self._file_manager.remove_file(FileDestinations.LOCAL, path)
                break
            except StorageError as e:
                self._logger.warning(
                    "Unable to overwrite the target file, it is currently in use.  Trying again in {0} seconds".format(
                        seconds_to_wait)
                )
                num_tries += 1
                if num_tries < max_tries:
                    time.sleep(seconds_to_wait)
                else:
                    self._logger.error(
                        "Reached max retries for deleting the source file.  Aborting."
                    )
                    raise e

    def _get_collision_free_filepath(self, path, display_name):
        directory, filename = self._file_manager.split_path(FileDestinations.LOCAL, path)
        extension = utilities.get_extension_from_filename(filename)
        filename_no_extension = utilities.remove_extension_from_filename(filename)

        display_name_no_extension = utilities.remove_extension_from_filename(display_name)

        original_filename = filename_no_extension
        original_display_name = display_name_no_extension
        file_number = 0
        # Check to see if the file exists, if it does add a number to the end and continue
        while self._file_manager.file_exists(
                FileDestinations.LOCAL,
                self._file_manager.join_path(
                    FileDestinations.LOCAL,
                    directory,
                    "{0}.{1}".format(filename_no_extension, extension)
                )
        ):
            file_number += 1
            filename_no_extension = "{0}_{1}".format(original_filename, file_number)
            display_name_no_extension = "{0}_{1}".format(original_display_name, file_number)

        return directory, "{0}.{1}".format(filename_no_extension, extension), "{0}.{1}".format(
            display_name_no_extension, extension)

    def copy_thumbnail(self, thumbnail_src, thumbnail_path, gcode_filename):
        # get the plugin implementation
        plugin_implementation = self._plugin_manager.get_plugin_info(thumbnail_src, True)
        if plugin_implementation:
            thumbnail_uri_root = 'plugin/' + thumbnail_src + '/thumbnail/'
            data_folder = plugin_implementation.implementation.get_plugin_data_folder()
            # extract the file name from the path
            path = thumbnail_path.replace(thumbnail_uri_root, '')
            querystring_index = path.rfind('?')
            if querystring_index > -1:
                path = path[0: querystring_index]

            path = os.path.join(data_folder, path)
            # see if the thumbnail exists
            if os.path.isfile(path):
                # create a new path
                pre, ext = os.path.splitext(gcode_filename)
                new_thumb_name = pre + ".png"
                new_path = os.path.join(data_folder, new_thumb_name)
                new_metadata = (
                        thumbnail_uri_root + new_thumb_name + "?" + "{:%Y%m%d%H%M%S}".format(
                    datetime.datetime.now()
                )
                )
                if path != new_path:
                    try:
                        copyfile(path, new_path)
                    except (IOError, OSError) as e:
                        self._logger.exception("An error occurred copying thumbnail from '%s' to '%s'", path, new_path)
                        self._logger.exception(e)
                        new_metadata = None
                return new_metadata
        return None

    def preprocessing_started(self, task):
        processor_args = task["processor_args"]
        octoprint_args = task["octoprint_args"]
        source_name = octoprint_args["source_name"]
        print_after_processing = octoprint_args["print_after_processing"]
        sendwebhook_after_processing = octoprint_args["sendwebhook_after_processing"]
        self._logger.info(
            "Starting pre-processing with the following arguments:"
            "\n\tsource_path: %s"
            "\n\tresolution_mm: %.3f"
            "\n\tpath_tolerance_percent: %.3f"
            "\n\textrusion_rate_variance_percent: %.3f"
            "\n\tmax_radius_mm: %d"
            "\n\tmm_per_arc_segment: %.3f"
            "\n\tmin_arc_segments: %d"
            "\n\tg90_g91_influences_extruder: %r"
            "\n\tallow_3d_arcs: %r"
            "\n\tallow_travel_arcs: %r"
            "\n\tallow_dynamic_precision: %r"
            "\n\tdefault_xyz_precision: %d"
            "\n\tdefault_e_precision: %d"
            "\n\tmax_gcode_length: %d"
            "\n\tlog_level: %d",
            processor_args["source_path"],
            processor_args["resolution_mm"],
            processor_args["path_tolerance_percent"],
            processor_args["extrusion_rate_variance_percent"],
            processor_args["max_radius_mm"],
            processor_args["min_arc_segments"],
            processor_args["mm_per_arc_segment"],
            processor_args["g90_g91_influences_extruder"],
            processor_args["allow_3d_arcs"],
            processor_args["allow_travel_arcs"],
            processor_args["allow_dynamic_precision"],
            processor_args["default_xyz_precision"],
            processor_args["default_e_precision"],
            processor_args["max_gcode_length"],
            processor_args["log_level"]
        )

        if print_after_processing:
            self._logger.info("The queued file will be printed after processing is complete.")
        if sendwebhook_after_processing:
            self._logger.info("The webhook will be sent after processing is complete.")

        data = {
            "message_type": "preprocessing-start",
            "message": "Preprocessing started for {0}".format(source_name),
            "task": task
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)
        self.send_preprocessing_tasks_update()

    def preprocessing_progress(self, progress, current_task):
        # Need to copy the dict else we will alter the original!  Might be a good idea to do this in the callback...
        data = copy.copy(progress)
        # remove the segment statistics text, it is large and currently unused
        data.pop("segment_statistics_text")
        data.pop("segment_travel_statistics_text")
        data.pop("segment_extrusion_statistics_text")
        data.pop("segment_retraction_statistics_text")

        # add the sorce and target name as well as the message type from the task
        data["source_name"] = current_task["octoprint_args"]["source_name"]
        data["target_name"] = current_task["octoprint_args"]["target_name"]
        data["message_type"] = "preprocessing-progress"

        self._plugin_manager.send_plugin_message(self._identifier, data)

    def preprocessing_cancelled(self, task, auto_cancelled):
        if task is not None:
            source_name = task["octoprint_args"]["source_name"]
            target_name = task["octoprint_args"]["target_name"]
            if auto_cancelled:
                message = "Cannot process while printing.  Arc Welding has been cancelled for '{0}'.  The file will be " \
                          "processed once printing has completed."
                if task.get("print_after_processing_cancelled", False):
                    message += "  'Print after Processing' has been cancelled for all items to protect your printer."
            else:
                message = "Preprocessing has been cancelled for '{0}'."

            message = message.format(source_name)
            data = {
                "message_type": "preprocessing-cancelled",
                "source_name": source_name,
                "target_name": target_name,
                "guid": task["guid"],
                "message": message
            }
        else:
            message = "All preprocessing tasks were cancelled."
            data = {
                "message_type": "all-preprocessing-cancelled",
                "message": message
            }
        self._plugin_manager.send_plugin_message(self._identifier, data)
        self.send_preprocessing_tasks_update()

    def sendwhdiscord(self, gcode_file_name, picture_file_name):
        import requests
        gcode_files_folder = self._discord_gcodePath
        picture_files_folder = self._discord_picturePath
        bot3dprinter_png = self._discord_botAvatar  # "bot3dprinter.png"
        thumbnail_png = self._discord_whThumbnails  # "s3dp_filigrane2.png"
        nick = self._discord_botName  # "Octoprint"
        discord_wh_url = self._discord_whUrl
        embeds = []
        embed = {
            "title": "3D Printer files monitor\n",
            "description": "A printable file have been uploaded: **%s**" % gcode_file_name,
            "type": "rich",
            "url": ("%s%s" % (gcode_files_folder, gcode_file_name)).replace(" ", "%20"),
            "thumbnail": {
                "url": "%s" % thumbnail_png
            },
            "image": {
                "url": ("%s%s" % (picture_files_folder, picture_file_name)).replace(" ", "%20")
            }
        }
        embeds.append(embed)

        data = {
            "avatar_url": "%s" % bot3dprinter_png,
            "username": nick,
            "embeds": embeds
        }
        self._logger.debug("sendwhdiscord data:")
        self._logger.debug(data)
        self._logger.debug("sendwhdiscord url: %s" % discord_wh_url)
        result = None
        try:
            result = requests.post(discord_wh_url, json=data)
            if 200 <= result.status_code < 300:
                self._logger.info("sendwhdiscord Webhook sent %s" % result.status_code)
            else:
                self._logger.exception(
                    "sendwhdiscord Webhook Not sent with %s, response: %s" % (result.status_code, str(result.content)))
        except Exception as e:
            self._logger.error("sendwhdiscord erreur:")
            self._logger.error(e)
            if result:
                self._logger.exception("sendwhdiscord Result %s" % result.status_code)

    def preprocessing_success(self, task, results):
        # extract the task data
        processor_args = task["processor_args"]
        octoprint_args = task["octoprint_args"]
        additional_metadata = octoprint_args["additional_metadata"]
        print_after_processing = octoprint_args["print_after_processing"]
        sendwebhook_after_processing = octoprint_args["sendwebhook_after_processing"]
        select_after_processing = octoprint_args["select_after_processing"]
        delete_after_processing = octoprint_args["delete_after_processing"]

        # save the newly created file.  This must be done before
        # exiting this callback because the target file isn't
        # guaranteed to exist later.
        try:
            metadata = self.save_preprocessed_file(
                task, results, additional_metadata
            )
        except TargetFileSaveError as e:
            self._logger.exception(e)

            data = {
                "message_type": "preprocessing-failed",
                "source_name": octoprint_args["source_name"],
                "target_name": octoprint_args["target_name"],
                "guid": task["guid"],
                "message": 'Unable to save the target file.  A file with the same name may be currently printing.'
            }
            self._plugin_manager.send_plugin_message(self._identifier, data)
            return

        if (
                delete_after_processing
                and self._file_manager.file_exists(FileDestinations.LOCAL, octoprint_args["source_path"])
                and octoprint_args["source_path"] != octoprint_args["target_path"]
        ):
            if not self._get_is_printing(octoprint_args["source_path"]):
                # if the file is selected, deselect it.
                if self._get_is_file_selected(octoprint_args["source_path"], FileDestinations.LOCAL):
                    self._printer.unselect_file()
                # delete the source file
                self._logger.info("Deleting source file at %s.", octoprint_args["source_path"])
                try:
                    self._file_manager.remove_file(FileDestinations.LOCAL, octoprint_args["source_path"])
                except octoprint.filemanager.storage.StorageError:
                    self._logger.exception("Unable to delete the source file at '%s'", octoprint_args["source_path"])
            else:
                self._logger.exception("Unable to delete the source file at '%s'.  It is currently printing.",
                                       processor_args["source_path"])

        data = {
            "message_type": "preprocessing-success",
            "arc_welder_statistics": metadata,
            "task": task
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)

        if task["octoprint_args"].get("cant_overwrite", False):
            self.send_notification_toast(
                "warning",
                "Unable to Overwrite Source File",
                "The source file is in use and cannot be overwritten.  The target file was renamed to '{0}'.".format(
                    task["octoprint_args"]["target_name"]),
                False,
                "unable-to-overwrite",
                []
            )

        if select_after_processing or print_after_processing:
            try:
                self._select_file(octoprint_args["target_path"])
                # make sure the file is selected
                if (
                        self._get_is_file_selected(octoprint_args["target_path"], FileDestinations.LOCAL)
                        and print_after_processing
                        and not self._get_is_printing()
                ):
                    self._printer.start_print(tags=set('arc_welder'))
            except (octoprint.printer.InvalidFileType, octoprint.printer.InvalidFileLocation):
                # we don't care too much if OctoPrint can't select the file.  There's nothing
                # we can do about it anyway
                pass

        # if sendwebhook_after_processing: # TODOgcode_file_name = octoprint_args["target_path"].split('/')[-1]
        self._logger.debug("upload_file start")
        self._logger.debug("upload_file %", octoprint_args["target_path"].split('/')[-1])
        self.upload_file(octoprint_args["target_path"].split('/')[-1])

    def preprocessing_completed(self, task):
        data = {
            "message_type": "preprocessing-complete",
            "guid": task["guid"],
            "preprocessing_tasks": self._preprocessor_worker.get_tasks()
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)

    def preprocessing_failed(self, task, message):
        data = {
            "message_type": "preprocessing-failed",
            "source_name": task["octoprint_args"]["source_name"],
            "target_name": task["octoprint_args"]["target_name"],
            "guid": task["guid"],
            "message": message
        }
        self._plugin_manager.send_plugin_message(self._identifier, data)

    def preprocessing_cancellations(self):
        cancel_all = self._cancel_all_processing
        guids_to_cancel = self._process_guids_to_cancel
        self._cancel_all_processing = False
        self._process_guids_to_cancel = []
        return cancel_all, guids_to_cancel

    def on_event(self, event, payload):

        if event == Events.PRINT_STARTED:
            current_process_cancelled, print_after_process_stopped = (
                self._preprocessor_worker.prevent_printing_for_existing_jobs()
            )
            queue_changed = current_process_cancelled or print_after_process_stopped
            if not current_process_cancelled and print_after_process_stopped:
                self.send_notification_toast(
                    "warning",
                    "Arc-Welder: Print After Processing Cancelled",
                    "A print is running, but processing tasks are in the queue that are marked 'Print After "
                    "Processing'. To protect your printer, Arc Welder will not automatically print when processing "
                    "is completed.",
                    True,
                    key="auto_print_cancelled", close_keys=["auto_print_cancelled"]
                )

            if queue_changed:
                self.send_preprocessing_tasks_update()

        elif event == Events.FILE_ADDED:

            # Note, 'target' is the key for FILE_UPLOADED, but 'storage' is the key for FILE_ADDED
            name = payload["name"]
            path = payload["path"]
            target = payload["storage"]

            self.add_file_to_preprocessor_queue(name, path, target, False)
        elif event == Events.PRINTER_STATE_CHANGED:
            if payload["state_id"] == "OPERATIONAL":
                if self._check_firmware_on_connect:
                    self._logger.info("Printer is connected and check firmware on connect is enabled." +
                                      " Checking Firmware.")
                    self.check_firmware()
                else:
                    self._logger.verbose("Printer is connected, but check firmware on connect is disabled." +
                                         " Skipping.")

    def get_additional_metadata(self, metadata):
        # list of supported metadata
        supported_metadata_keys = ['thumbnail', 'thumbnail_src']
        additional_metadata = {}
        # Create the additional metadata from the supported keys
        for key in supported_metadata_keys:
            if key in metadata:
                additional_metadata[key] = metadata[key]
        return additional_metadata

    def get_preprocessor_task(self, source_name, source_path, gcode_comment_settings, is_manual_request, metadata):
        # Start building up the feature settings, but override with any gcode comment settings
        # This option doesn't work atm, maybe in the future
        # uploaded_by_slicer = gcode_comment_settings.get("slicer_upload_type", "") != ""
        select_after_processing = self._get_select_after_processing(not is_manual_request)
        select_after_processing = gcode_comment_settings.get("select", select_after_processing)
        delete_after_processing = self._get_delete_source()
        delete_after_processing = gcode_comment_settings.get("delete", delete_after_processing)
        # we can't delete if the pre and postfix are both overridden and empty
        if (
                delete_after_processing
                and gcode_comment_settings.get("prefix", "NOTFOUND") == ""
                and gcode_comment_settings.get("postfix", "NOTFOUND") == ""
        ):
            self._logger.debug("Cannot delete after processing due to source overwrite.")
            delete_after_processing = False

        # Never printer after completion if we are currently printing.
        # Otherwise, check the plugin settings
        print_after_processing = False
        if not self._get_is_printing():
            # first, get this value based on the settings
            print_after_processing = self._get_print_after_processing(is_manual_request)
            # now override if the WELD paramater is supplied within the gcode
            print_after_processing = gcode_comment_settings.get("print", print_after_processing)
        additional_metadata = self.get_additional_metadata(metadata)
        source_path_on_disk = self._file_manager.path_on_disk(FileDestinations.LOCAL, source_path)
        # make sure the path starts with a / for compatibility
        if source_path[0] != '/':
            source_path = '/' + source_path

        # Gcode comment settings will override those from the interface.  Get them one at a time, and test the values

        resolution_mm = gcode_comment_settings.get("resolution_mm", self._resolution_mm)
        if resolution_mm > 0.5:
            self._logger.warning(
                "The resolution_mm setting  %0.2f is greater than the recommended max of 0.5mm",
                resolution_mm
            )

        path_tolerance_percent = gcode_comment_settings.get("path_tolerance_percent", self._path_tolerance_percent)
        if path_tolerance_percent > 5:
            self._logger.warning(
                "The path tolerance percent %0.2f percent is greater than the recommended max of 5%%",
                path_tolerance_percent
            )

        extrusion_rate_variance_detection_enabled = gcode_comment_settings.get(
            "extrusion_rate_variance_detection_enabled",
            self._extrusion_rate_variance_detection_enabled
        )
        extrusion_rate_variance_percent = 0
        if extrusion_rate_variance_detection_enabled:
            extrusion_rate_variance_percent = gcode_comment_settings.get(
                "extrusion_rate_variance_percent",
                self._extrusion_rate_variance_percent
            )
            if extrusion_rate_variance_percent < 0:
                self._logger.warning(
                    "The extrusion rate tolerance percent of %0.2f is less than 0, and has been set to the default.",
                    extrusion_rate_variance_percent
                )
                extrusion_rate_variance_percent = self.settings_default.get("extrusion_rate_variance_percent")

        max_radius_mm = gcode_comment_settings.get("max_radius_mm", self._max_radius_mm)
        if max_radius_mm > 1000000:
            self._logger.warning(
                "The max radius mm of %0.2fmm is greater than the recommended max of 1000000mm (1km).",
                max_radius_mm
            )
        elif max_radius_mm <= 0:
            self._logger.warning(
                "The max radius mm of %0.2fmm is less than or equal to zero.  Setting to the default value.",
                max_radius_mm
            )
            max_radius_mm = self.settings_default.get("max_radius_mm")

        firmware_compensation_enabled = gcode_comment_settings.get(
            "firmware_compensation_enabled", self._firmware_compensation_enabled
        )

        if not firmware_compensation_enabled:
            min_arc_segments = 0
            mm_per_arc_segment = 0
        else:
            min_arc_segments = gcode_comment_settings.get("min_arc_segments", self._min_arc_segments)
            if min_arc_segments < 0:
                self._logger.warning(
                    "The min arc segments value is less than 0.  Setting to 0, which will disable firmware compensation."
                )
                min_arc_segments = 0

            mm_per_arc_segment = gcode_comment_settings.get("mm_per_arc_segment", self._mm_per_arc_segment)
            if mm_per_arc_segment < 0:
                self._logger.warning(
                    "The mm per arc segment value is less than 0.  Setting to 0, which will disable firmware compensation."
                )
                mm_per_arc_segment = 0

        allow_3d_arcs = gcode_comment_settings.get(
            "allow_3d_arcs", self._allow_3d_arcs
        )

        allow_travel_arcs = gcode_comment_settings.get(
            "allow_travel_arcs", self._allow_travel_arcs
        )

        g90_g91_influences_extruder = gcode_comment_settings.get(
            "g90_g91_influences_extruder", self._g90_g91_influences_extruder
        )

        allow_dynamic_precision = gcode_comment_settings.get(
            "allow_dynamic_precision", self._allow_dynamic_precision
        )

        default_xyz_precision = gcode_comment_settings.get(
            "default_xyz_precision", self._default_xyz_precision
        )

        if not default_xyz_precision:
            self._logger.warning(
                "The default XYZ precision was not specified.  Setting to the default value."
            )
            default_xyz_precision = self.settings_default.get("default_xyz_precision")
        elif default_xyz_precision < 3:
            self._logger.warning(
                "The default XYZ precision is less than 3.  Setting to the default value."
            )
            default_xyz_precision = self.settings_default.get("default_xyz_precision")
        elif default_xyz_precision > 6:
            self._logger.warning(
                "The default XYZ precision is greater than 6, setting to 6."
            )
            default_xyz_precision = 6

        default_e_precision = gcode_comment_settings.get(
            "default_e_precision", self._default_e_precision
        )
        if not default_e_precision:
            self._logger.warning(
                "The default E precision was not specified.  Setting to the default value."
            )
            default_e_precision = 5
        elif default_e_precision < 3:
            self._logger.warning(
                "The default E precision is less than 3.  Setting to the default value."
            )
            default_e_precision = 3
        elif default_e_precision > 6:
            self._logger.warning(
                "The default E precision is greater than 6, setting to 6."
            )
            default_e_precision = 6

        max_gcode_length_detection_enabled = gcode_comment_settings.get(
            "max_gcode_length_detection_enabled",
            self._max_gcode_length_detection_enabled
        )
        max_gcode_length = 0
        if max_gcode_length_detection_enabled:
            max_gcode_length = gcode_comment_settings.get(
                "max_gcode_length", self._max_gcode_length
            )
            if max_gcode_length < 0:
                self._logger.warning(
                    "The max g2/g3 length setting is less than 0.  Setting to 0, which will disable max g2 g3 length detection.")
                max_gcode_length = 0
            elif max_gcode_length < 31:
                self._logger.warning(
                    "The max g2/g3 length setting is less than 31, but not 0.  Setting to 0, which will disable max g2 g3 length detection.")
                max_gcode_length = 0

        # determine the target file name and path
        sendwebhook_after_processing = self._get_sendwebhook_after_processing()

        target_name, target_path, target_display_name = self.get_output_file_name_and_path(source_name, source_path,
                                                                                           gcode_comment_settings)
        return {
            "octoprint_args": {
                "source_name": source_name,
                "source_path": source_path,
                "target_name": target_name,
                "target_path": target_path,
                "target_display_name": target_display_name,
                "additional_metadata": additional_metadata,
                "is_manual_request": is_manual_request,
                "print_after_processing": print_after_processing,
                "sendwebhook_after_processing": sendwebhook_after_processing,
                "select_after_processing": select_after_processing,
                "delete_after_processing": delete_after_processing
            },
            "processor_args": {
                "source_path": source_path_on_disk,
                "resolution_mm": resolution_mm,
                "path_tolerance_percent": path_tolerance_percent / 100.0,  # Convert to decimal percent
                "extrusion_rate_variance_percent": extrusion_rate_variance_percent / 100.0,
                # Convert to decimal percent
                "max_radius_mm": max_radius_mm,
                "min_arc_segments": min_arc_segments,
                "mm_per_arc_segment": mm_per_arc_segment,
                "g90_g91_influences_extruder": g90_g91_influences_extruder,
                "allow_3d_arcs": allow_3d_arcs,
                "allow_travel_arcs": allow_travel_arcs,
                "allow_dynamic_precision": allow_dynamic_precision,
                "default_xyz_precision": default_xyz_precision,
                "default_e_precision": default_e_precision,
                "max_gcode_length": max_gcode_length,
                "log_level": self._gcode_conversion_log_level
            }
        }

    def add_file_to_preprocessor_queue(self,
                                       source_name,
                                       source_path,
                                       source_target,
                                       is_manual_request
                                       ):
        # There are a lot of checks we need to do before processing
        # we can just exit in many caess
        if not self._enabled:
            # if Arc Welder is disabled, don't do jack :)
            return

        if source_target != FileDestinations.LOCAL:
            # We can't process files on SD
            self._logger.debug("Cannot process '%s', it is on SD.", source_name)
            return

        if not octoprint.filemanager.valid_file_type(source_path, type="gcode"):
            # Not a gcode file, exit
            self._logger.debug("Cannot process '%s', it is not a gcode file.", source_name)
            return

        # Now we know we should process!
        # Extract only the supported metadata from the added file
        metadata = self._file_manager.get_metadata(source_target, source_path)

        # got a report that this delivers NULL sometimes.
        if metadata is None:
            metadata = {}

        if "arc_welder" in metadata:
            # This has been welded, exit
            self._logger.debug("Cannot process '%s', it has already been welded.", source_name)
            return

        path_on_disk = self._file_manager.path_on_disk(FileDestinations.LOCAL, source_path)

        # now we need to do an expensive search of the file to check for the following:
        # 1. Was this file welded as indicated by gcode comments?
        # 2. Was this file uploaded from a slicer (experimental, mostly non-functional as yet)
        # 3. Does this file have any ArcWelder comment settings
        gcode_search_results = utilities.search_gcode_file(
            path_on_disk,
            [
                ArcWelderPlugin.SEARCH_FUNCTION_IS_WELDED,
                # Can't do this yet.
                # ArcWelderPlugin.SEARCH_FUNCTION_CURA_UPLOAD,
                ArcWelderPlugin.SEARCH_FUNCTION_SETTINGS
            ]
        )
        # pull out the interesting bits of the file info that tell us if we should process or not
        gcode_comment_settings = {}
        if gcode_search_results:
            if gcode_search_results.get("is_welded", False):
                self._logger.info(
                    "Cannot process '%s', a gcode search indicates that it has already been welded.", source_name
                )

                self._file_manager.set_additional_metadata(
                    FileDestinations.LOCAL, source_path, "arc_welder", True, overwrite=False, merge=False
                )

                # this file was welded, it would be a waste to weld again
                return
            if "settings" in gcode_search_results:
                gcode_comment_settings = gcode_search_results["settings"]

        if not gcode_comment_settings.get("weld", False):
            # If the gcode file is set to weld, we don't want to enforce any of this
            if not self._get_process_file(is_manual_request):
                self._logger.debug("Cannot weld '%s', Welding is not enabled for uploaded files.", source_name)
                # not set to auto process regularly uploaded file, exit
                return

        self._logger.info("Adding %s to processor queue", source_path)

        task = self.get_preprocessor_task(source_name, source_path, gcode_comment_settings, is_manual_request, metadata)
        results = self._preprocessor_worker.add_task(task)
        if not results["success"]:
            self.send_notification_toast(
                "warning", "Arc-Welder: Unable To Queue File",
                results["error_message"],
                True,
                key="unable_to_queue", close_keys=["unable_to_queue"]
            )
            return False

        # if we are going to overwrite or delete the target file, cancel preprocessing
        if (
                task["octoprint_args"]["delete_after_processing"] or
                task["octoprint_args"]["source_path"] == task["octoprint_args"]["target_path"]
        ):
            # these are private members, make sure they exist
            if hasattr(self._file_manager, "_analysis_queue_entry") and \
                    hasattr(self._file_manager, "_analysis_queue"):
                try:
                    queue_entry = self._file_manager._analysis_queue_entry(
                        FileDestinations.LOCAL, task["octoprint_args"]["source_path"]
                    )
                    self._file_manager._analysis_queue.dequeue(queue_entry)
                except Exception as e:
                    # this may be too broad, but I don't want any errors here!
                    self._logger.exception(e)
                    self._logger.exception("Unable to remove the currently processing file from the analysis queue.")

        if self._show_queued_notification:
            message = "Successfully queued {0} for processing.".format(task["octoprint_args"]["source_name"])
            if self._printer.is_printing():
                message += " Processing will occurr after the current print is completed." + \
                           "The 'Print After Processing' option will be ignored."

            data = {
                "message_type": "task-queued",
                "message": message
            }
            self._plugin_manager.send_plugin_message(self._identifier, data)

        self.send_preprocessing_tasks_update()
        return True

    def register_custom_routes(self, server_routes, *args, **kwargs):
        # version specific permission validator
        if Version(octoprint.server.VERSION) >= Version("1.4"):
            admin_validation_chain = [
                util.tornado.access_validation_factory(app, util.flask.admin_validator),
            ]
        else:
            # the concept of granular permissions does not exist in this version of Octoprint.  Fallback to the
            # admin role
            def admin_permission_validator(flask_request):
                user = util.flask.get_flask_user_from_request(flask_request)
                if user is None or not user.is_authenticated() or not user.is_admin():
                    raise tornado.web.HTTPError(403)

            permission_validator = admin_permission_validator
            admin_validation_chain = [util.tornado.access_validation_factory(app, permission_validator), ]
        return [
            (
                r"/downloadFile",
                ArcWelderLargeResponseHandler,
                dict(
                    request_callback=self.download_file_request,
                    as_attachment=True,
                    access_validation=util.tornado.validation_chain(*admin_validation_chain)
                )

            )
        ]

        # ~~ software update hook

    arc_welder_update_info = dict(
        displayName="Arc Welder: Anti-Stutter",
        # version check: github repository
        type="github_release",
        user="mtaliancich",
        repo="ArcWelderPlugin",
        pip="https://github.com/MTaliancich/ArcWelderPlugin/archive/{target_version}.zip",
        stable_branch=dict(branch=MASTER, commitish=[MASTER], name="Stable"),
        release_compare='custom',
        prerelease_branches=[
            dict(
                branch=RC_MAINTENANCE,
                commitish=[("%s" % MASTER), RC_MAINTENANCE],  # maintenance RCs (include master)
                name="Maintenance RCs"
            ),
            dict(
                branch=RC_DEVEL,
                commitish=[MASTER, RC_MAINTENANCE, RC_DEVEL],  # devel & maintenance RCs (include master)
                name="Devel RCs"
            )
        ],
    )

    def get_release_info(self):
        # Starting with V1.5.0 prerelease branches are supported!
        if Version(octoprint.server.VERSION) < Version("1.5.0"):
            # Hack to attempt to get pre-release branches to work prior to 1.5.0
            # get the checkout type from the software updater
            prerelease_channel = None
            is_prerelease = False
            # get this for reference.  Eventually I'll have to use it!
            # is the software update set to prerelease?

            if self._settings.global_get(["plugins", "softwareupdate", "checks", "octoprint", "prerelease"]):
                # If it's a prerelease, look at the channel and configure the proper branch for Arc Welder
                prerelease_channel = self._settings.global_get(
                    ["plugins", "softwareupdate", "checks", "octoprint", "prerelease_channel"]
                )
                if prerelease_channel == RC_MAINTENANCE:
                    is_prerelease = True
                    prerelease_channel = RC_MAINTENANCE
                elif prerelease_channel == RC_DEVEL:
                    is_prerelease = True
                    prerelease_channel = RC_DEVEL

            ArcWelderPlugin.arc_welder_update_info["prerelease"] = is_prerelease
            if prerelease_channel is not None:
                ArcWelderPlugin.arc_welder_update_info["prerelease_channel"] = prerelease_channel

        ArcWelderPlugin.arc_welder_update_info["displayVersion"] = self._plugin_version
        ArcWelderPlugin.arc_welder_update_info["current"] = self._plugin_version
        return dict(
            arc_welder=ArcWelderPlugin.arc_welder_update_info
        )

    def get_update_information(self):
        # moved most of the heavy lifting to get_latest, since I need to do a custom version compare.
        # AND I want to use the most recent software update release channel settings.
        return self.get_release_info()

    # noinspection PyUnusedLocal
    def on_gcode_sent(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if self._enabled and self._firmware_checker is not None:
            try:
                self._firmware_checker.on_gcode_sending(comm_instance, phase, cmd, cmd_type, gcode, args, kwargs)
            except Exception as e:
                self._logger.exception(e)
                self._logger.exception("on_gcode_sent failed.")
        return None

    # noinspection PyUnusedLocal
    def on_gcode_received(self, comm, line, *args, **kwargs):
        if self._enabled and self._firmware_checker is not None:
            try:
                return self._firmware_checker.on_gcode_received(comm, line, args, kwargs)
            except Exception as e:
                self._logger.exception(e)
                self._logger.exception("on_gcode_received failed.")
        return line


__plugin_pythoncompat__ = ">=2.7,<4"
__plugin_implementation__ = ArcWelderPlugin()


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = ArcWelderPlugin()
    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.server.http.routes": __plugin_implementation__.register_custom_routes,
        "octoprint.comm.protocol.gcode.received": (__plugin_implementation__.on_gcode_received, -1),
        "octoprint.comm.protocol.gcode.sent": (__plugin_implementation__.on_gcode_sent, -1),
    }


class ArcWelderLargeResponseHandler(LargeResponseHandler):

    def initialize(self,
                   request_callback,
                   as_attachment=False,
                   access_validation=None,
                   default_filename=None,
                   on_before_request=None,
                   on_after_request=None,
                   **kwargs):
        super(ArcWelderLargeResponseHandler, self).initialize(
            '',
            default_filename=default_filename,
            as_attachment=as_attachment,
            allow_client_caching=False,
            access_validation=access_validation,
            path_validation=None, etag_generator=None,
            name_generator=self.name_generator,
            mime_type_guesser=None
        )
        self.download_file_name = None
        self._before_request_callback = on_before_request
        self._request_callback = request_callback
        self._after_request_callback = on_after_request
        self.after_request_internal = None
        self.after_request_internal_args = None

    def name_generator(self, path):
        if self.download_file_name is not None:
            return self.download_file_name

    def prepare(self):
        if self._before_request_callback:
            self._before_request_callback()

    def get(self, include_body=True):
        if self._access_validation is not None:
            self._access_validation(self.request)

        if "cookie" in self.request.arguments:
            self.set_cookie(self.request.arguments["cookie"][0], "true", path="/")
        full_path = self._request_callback(self)
        self.root = os.path.dirname(full_path)

        # if the file does not exist, return a 404
        if not os.path.isfile(full_path):
            raise tornado.web.HTTPError(404)

        # return the file
        return tornado.web.StaticFileHandler.get(self, full_path, include_body=include_body)

    def on_finish(self):
        if self.after_request_internal:
            self.after_request_internal(**self.after_request_internal_args)

        if self._after_request_callback:
            self._after_request_callback()


class TargetFileSaveError(Exception):
    pass


from ._version import get_versions

__version__ = get_versions()["version"]
__git_version__ = get_versions()["full-revisionid"]
del get_versions
