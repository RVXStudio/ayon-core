import os
from os.path import getsize
import logging
import sys
import copy
import clique
import errno
import six
import re

from pymongo import DeleteOne, InsertOne, UpdateOne
import pyblish.api
from avalon import io
from avalon.api import format_template_with_optional_keys
import openpype.api
from datetime import datetime
# from pype.modules import ModulesManager
from openpype.lib.profiles_filtering import filter_profiles
from openpype.lib import (
    prepare_template_data,
    create_hard_link
)

# this is needed until speedcopy for linux is fixed
if sys.platform == "win32":
    from speedcopy import copyfile
else:
    from shutil import copyfile

log = logging.getLogger(__name__)


def get_frame_padded(frame, padding):
    """Return frame number as string with `padding` amount of padded zeros"""
    return "{frame:0{padding}d}".format(padding=padding, frame=frame)


def get_first_frame_padded(collection):
    """Return first frame as padded number from `clique.Collection`"""
    start_frame = next(iter(collection.indexes))
    return get_frame_padded(start_frame, padding=collection.padding)


class IntegrateAssetNew(pyblish.api.InstancePlugin):
    """Resolve any dependency issues

    This plug-in resolves any paths which, if not updated might break
    the published file.

    The order of families is important, when working with lookdev you want to
    first publish the texture, update the texture paths in the nodes and then
    publish the shading network. Same goes for file dependent assets.

    Requirements for instance to be correctly integrated

    instance.data['representations'] - must be a list and each member
    must be a dictionary with following data:
        'files': list of filenames for sequence, string for single file.
                 Only the filename is allowed, without the folder path.
        'stagingDir': "path/to/folder/with/files"
        'name': representation name (usually the same as extension)
        'ext': file extension
    optional data
        "frameStart"
        "frameEnd"
        'fps'
        "data": additional metadata for each representation.
    """

    label = "Integrate Asset New"
    order = pyblish.api.IntegratorOrder
    families = ["workfile",
                "pointcache",
                "camera",
                "animation",
                "model",
                "mayaAscii",
                "mayaScene",
                "setdress",
                "layout",
                "ass",
                "vdbcache",
                "scene",
                "vrayproxy",
                "vrayscene_layer",
                "render",
                "prerender",
                "imagesequence",
                "review",
                "rendersetup",
                "rig",
                "plate",
                "look",
                "audio",
                "yetiRig",
                "yeticache",
                "nukenodes",
                "gizmo",
                "source",
                "matchmove",
                "image",
                "source",
                "assembly",
                "fbx",
                "textures",
                "action",
                "harmony.template",
                "harmony.palette",
                "editorial",
                "background",
                "camerarig",
                "redshiftproxy",
                "effect",
                "xgen",
                "hda",
                "usd"
                ]
    exclude_families = ["clip"]
    db_representation_context_keys = [
        "project", "asset", "task", "subset", "version", "representation",
        "family", "hierarchy", "task", "username", "frame", "udim"
    ]
    default_template_name = "publish"

    # suffix to denote temporary files, use without '.'
    TMP_FILE_EXT = 'tmp'

    # file_url : file_size of all published and uploaded files
    destinations = list()

    # Attributes set by settings
    template_name_profiles = None
    subset_grouping_profiles = None

    def process(self, instance):
        self.destinations = []

        # Exclude instances that also contain families from exclude families
        families = set(
            # Consider family and families data
            [instance.data["family"]] + instance.data.get("families", [])
        )
        if families & set(self.exclude_families):
            return

        try:
            self.register(instance)
            self.log.info("Integrated Asset in to the database ...")
            self.handle_destination_files(self.destinations,
                                          'finalize')
        except Exception:
            # clean destination
            self.log.critical("Error when registering", exc_info=True)
            self.handle_destination_files(self.destinations, 'remove')
            six.reraise(*sys.exc_info())

    def prepare_anatomy(self, instance):
        """Prepare anatomy data used to define representation destinations"""

        context = instance.context

        anatomy_data = instance.data["anatomyData"]
        project_entity = instance.data["projectEntity"]

        context_asset_name = None
        context_asset_doc = context.data.get("assetEntity")
        if context_asset_doc:
            context_asset_name = context_asset_doc["name"]

        asset_name = instance.data["asset"]
        asset_entity = instance.data.get("assetEntity")
        if not asset_entity or asset_entity["name"] != context_asset_name:
            asset_entity = io.find_one({
                "type": "asset",
                "name": asset_name,
                "parent": project_entity["_id"]
            })
            assert asset_entity, (
                "No asset found by the name \"{0}\" in project \"{1}\""
            ).format(asset_name, project_entity["name"])

            instance.data["assetEntity"] = asset_entity

            # update anatomy data with asset specific keys
            # - name should already been set
            hierarchy = ""
            parents = asset_entity["data"]["parents"]
            if parents:
                hierarchy = "/".join(parents)
            anatomy_data["hierarchy"] = hierarchy

        # Make sure task name in anatomy data is same as on instance.data
        asset_tasks = (
            asset_entity.get("data", {}).get("tasks")
        ) or {}
        task_name = instance.data.get("task")
        if task_name:
            task_info = asset_tasks.get(task_name) or {}
            task_type = task_info.get("type")

            project_task_types = project_entity["config"]["tasks"]
            task_code = project_task_types.get(task_type, {}).get("short_name")
            anatomy_data["task"] = {
                "name": task_name,
                "type": task_type,
                "short": task_code
            }

        elif "task" in anatomy_data:
            # Just set 'task_name' variable to context task
            task_name = anatomy_data["task"]["name"]
            task_type = anatomy_data["task"]["type"]

        else:
            task_name = None
            task_type = None

        # Fill family in anatomy data
        anatomy_data["family"] = instance.data.get("family")

        intent_value = instance.context.data.get("intent")
        if intent_value and isinstance(intent_value, dict):
            intent_value = intent_value.get("value")

        if intent_value:
            anatomy_data["intent"] = intent_value

        # Get profile
        key_values = {
            "families": self.main_family_from_instance(instance),
            "tasks": task_name,
            "hosts": instance.context.data["hostName"],
            "task_types": task_type
        }
        profile = filter_profiles(
            self.template_name_profiles,
            key_values,
            logger=self.log
        )

        template_name = "publish"
        if profile:
            template_name = profile["template_name"]

        return template_name, anatomy_data

    def register(self, instance):

        instance_stagingdir = instance.data.get("stagingDir")
        if not instance_stagingdir:
            self.log.info((
                "{0} is missing reference to staging directory."
                " Will try to get it from representation."
            ).format(instance))

        else:
            self.log.debug(
                "Establishing staging directory "
                "@ {0}".format(instance_stagingdir)
            )

        # Ensure at least one file is set up for transfer in staging dir.
        repres = instance.data.get("representations")
        assert repres, "Instance has no files to transfer"
        assert isinstance(repres, (list, tuple)), (
            "Instance 'files' must be a list, got: {0} {1}".format(
                str(type(repres)), str(repres)
            )
        )

        subset = self.register_subset(instance)

        version = self.register_version(instance, subset)
        instance.data["versionEntity"] = version
        instance.data['version'] = version['name']

        existing_repres = list(io.find({
            "parent": version["_id"],
            "type": "archived_representation"
        }))

        # Find the representations to transfer amongst the files
        # Each should be a single representation (as such, a single extension)
        template_name, anatomy_data = self.prepare_anatomy(instance)
        published_representations = {}
        representations = []
        for repre in instance.data["representations"]:

            if "delete" in repre.get("tags", []):
                self.log.debug("Skipping representation marked for deletion: "
                               "{}".format(repre))
                continue

            prepared = self.prepare_representation(repre,
                                                   anatomy_data,
                                                   template_name,
                                                   existing_repres,
                                                   version,
                                                   instance_stagingdir,
                                                   instance)

            # todo: simplify this?
            representation = prepared["representation"]
            representations.append(representation)
            published_representations[representation["_id"]] = prepared

        # Remove old representations if there are any (before insertion of new)
        if existing_repres:
            repre_ids_to_remove = [repre["_id"] for repre in existing_repres]
            io.delete_many({"_id": {"$in": repre_ids_to_remove}})

        # Write the new representations to the database
        io.insert_many(representations)

        instance.data["published_representations"] = published_representations

        self.log.info("Registered {} representations"
                      "".format(len(representations)))

    def register_version(self, instance, subset):

        version_number = instance.data["version"]
        self.log.debug("Next version: v{}".format(version_number))

        version_data = self.create_version_data(instance)
        version_data_instance = instance.data.get('versionData')
        if version_data_instance:
            version_data.update(version_data_instance)

        version = {
            "schema": "openpype:version-3.0",
            "type": "version",
            "parent": subset["_id"],
            "name": version_number,
            "data": version_data
        }

        repres = instance.data.get("representations", [])
        new_repre_names_low = [_repre["name"].lower() for _repre in repres]

        existing_version = io.find_one({
            'type': 'version',
            'parent': subset["_id"],
            'name': version_number
        })

        if existing_version is None:
            self.log.debug("Creating new version ...")
            version_id = io.insert_one(version).inserted_id
        else:
            self.log.debug("Updating existing version ...")
            # Check if instance have set `append` mode which cause that
            # only replicated representations are set to archive
            append_repres = instance.data.get("append", False)
            bulk_writes = []

            # Update version data
            version_id = existing_version['_id']
            bulk_writes.append(UpdateOne({
                '_id': version_id
            }, {
                '$set': version
            }))

            # Find representations of existing version and archive them
            current_repres = io.find({
                "type": "representation",
                "parent": version_id
            })
            for repre in current_repres:
                if append_repres:
                    # archive only duplicated representations
                    if repre["name"].lower() not in new_repre_names_low:
                        continue
                # Representation must change type,
                # `_id` must be stored to other key and replaced with new
                # - that is because new representations should have same ID
                repre_id = repre["_id"]
                bulk_writes.append(DeleteOne({"_id": repre_id}))

                repre["orig_id"] = repre_id
                repre["_id"] = io.ObjectId()
                repre["type"] = "archived_representation"
                bulk_writes.append(InsertOne(repre))

            # bulk updates
            if bulk_writes:
                io._database[io.Session["AVALON_PROJECT"]].bulk_write(
                    bulk_writes
                )

        version = io.find_one({"_id": version_id})
        return version

    def prepare_representation(self, repre,
                               anatomy_data,
                               template_name,
                               existing_repres,
                               version,
                               instance_stagingdir,
                               instance):

        # create template data for Anatomy
        template_data = copy.deepcopy(anatomy_data)

        # pre-flight validations
        if repre["ext"].startswith("."):
            raise ValueError("Extension must not start with a dot '.': "
                             "{}".format(repre["ext"]))

        if repre.get("transfers"):
            raise ValueError("Representation is not allowed to have transfers"
                             "data before integration. "
                             "Got: {}".format(repre["transfers"]))

        # required representation keys
        files = repre['files']
        template_data["representation"] = repre["name"]
        template_data["ext"] = repre["ext"]

        # optionals
        # retrieve additional anatomy data from representation if exists
        for representation_key, anatomy_key in {
            # Representation Key: Anatomy data key
            "resolutionWidth": "resolution_width",
            "resolutionHeight": "resolution_height",
            "fps": "fps",
            "outputName": "output",
        }.items():
            value = repre.get(representation_key)
            if value:
                template_data[anatomy_key] = value

        if repre.get('stagingDir'):
            stagingdir = repre['stagingDir']
        else:
            # Fall back to instance staging dir if not explicitly
            # set for representation in the instance
            self.log.debug("Representation uses instance staging dir: "
                           "{}".format(instance_stagingdir))
            stagingdir = instance_stagingdir

        self.log.debug("Anatomy template name: {}".format(template_name))
        anatomy = instance.context.data['anatomy']
        template = os.path.normpath(
            anatomy.templates[template_name]["path"])

        is_sequence_representation = isinstance(files, (list, tuple))
        if is_sequence_representation:
            # Collection of files (sequence)
            # Get the sequence as a collection. The files must be of a single
            # sequence and have no remainder outside of the collections.
            collections, remainder = clique.assemble(files,
                                                     minimum_items=1)
            if not collections:
                raise ValueError("No collections found in files: "
                                 "{}".format(files))
            if remainder:
                raise ValueError("Files found not detected as part"
                                 " of a sequence: {}".format(remainder))
            if len(collections) > 1:
                raise ValueError("Files in sequence are not part of a"
                                 " single sequence collection: "
                                 "{}".format(collections))
            src_collection = collections[0]

            # If the representation has `frameStart` set it renumbers the
            # frame indices of the published collection. It will start from
            # that `frameStart` index instead. Thus if that frame start
            # differs from the collection we want to shift the destination
            # frame indices from the source collection.
            destination_indexes = list(src_collection.indexes)
            destination_padding = len(get_first_frame_padded(src_collection))
            if repre.get("frameStart") is not None:
                index_frame_start = int(repre.get("frameStart"))

                # TODO use frame padding from right template group
                render_template = anatomy.templates["render"]
                frame_start_padding = int(
                    render_template.get(
                        "frame_padding",
                        render_template.get("padding")
                    )
                )

                # Shift destination sequence to the start frame
                src_start_frame = next(iter(src_collection.indexes))
                shift = index_frame_start - src_start_frame
                if shift:
                    destination_indexes = [
                        frame + shift for frame in destination_indexes
                    ]
                destination_padding = frame_start_padding

            # To construct the destination template with anatomy we require
            # a Frame or UDIM tile set for the template data. We use the first
            # index of the destination for that because that could've shifted
            # from the source indexes, etc.
            first_index_padded = get_frame_padded(frame=destination_indexes[0],
                                                  padding=destination_padding)
            if repre.get("udim"):
                # UDIM representations handle ranges in a different manner
                template_data["udim"] = first_index_padded
            else:
                template_data["frame"] = first_index_padded

            # Construct destination collection from template
            anatomy_filled = anatomy.format(template_data)
            template_filled = anatomy_filled[template_name]["path"]
            repre_context = template_filled.used_values
            self.log.debug("Template filled: {}".format(str(template_filled)))
            dst_collections, _remainder = clique.assemble(
                [os.path.normpath(template_filled)], minimum_items=1
            )
            assert not _remainder, "This is a bug"
            assert len(dst_collections) == 1, "This is a bug"
            dst_collection = dst_collections[0]

            # Update the destination indexes and padding
            dst_collection.indexes = destination_indexes
            dst_collection.padding = destination_padding
            assert len(src_collection) == len(dst_collection), "This is a bug"

            transfers = []
            for src_file_name, dst in zip(src_collection, dst_collection):
                src = os.path.join(stagingdir, src_file_name)
                self.log.debug("source: {}".format(src))
                self.log.debug("destination: `{}`".format(dst))
                transfers.append(src, dst)

            # Store first frame as published path
            # todo: remove `published_path` since it can be retrieved from
            #       `transfers` by taking the first destination transfers[0][1]
            repre['published_path'] = next(iter(dst_collection))
            repre["transfers"].extend(transfers)

        else:
            # Single file
            template_data.pop("frame", None)
            fname = files
            assert not os.path.isabs(fname), (
                "Given file name is a full path"
            )
            # Store used frame value to template data
            if repre.get("udim"):
                template_data["udim"] = repre["udim"][0]
            src = os.path.join(stagingdir, fname)
            anatomy_filled = anatomy.format(template_data)
            template_filled = anatomy_filled[template_name]["path"]
            repre_context = template_filled.used_values
            dst = os.path.normpath(template_filled)

            # Single file transfer
            self.log.debug("source: {}".format(src))
            self.log.debug("destination: `{}`".format(dst))
            repre["transfers"] = [src, dst]

            repre['published_path'] = dst

        if repre.get("udim"):
            repre_context["udim"] = repre.get("udim")  # store list

        for key in self.db_representation_context_keys:
            value = template_data.get(key)
            if not value:
                continue
            repre_context[key] = template_data[key]

        # Use previous representation's id if there are any
        repre_id = None
        repre_name_lower = repre["name"].lower()
        for _existing_repre in existing_repres:
            # NOTE should we check lowered names?
            if repre_name_lower == _existing_repre["name"].lower():
                repre_id = _existing_repre["orig_id"]
                break

        # Create new id if existing representations does not match
        if repre_id is None:
            repre_id = io.ObjectId()

        # todo: `repre` is not the actual `representation` entity
        #       we should simplify/clarify difference between data above
        #       and the actual representation entity for the database
        data = repre.get("data") or {}
        data.update({'path': dst, 'template': template})
        representation = {
            "_id": repre_id,
            "schema": "openpype:representation-2.0",
            "type": "representation",
            "parent": version["_id"],
            "name": repre['name'],
            "data": data,
            "dependencies": instance.data.get("dependencies", "").split(),

            # Imprint shortcut to context for performance reasons.
            "context": repre_context
        }

        if repre.get("outputName"):
            representation["context"]["output"] = repre['outputName']

        if is_sequence_representation and repre.get("frameStart") is not None:
            representation['context']['frame'] = template_data["frame"]

        # any file that should be physically copied is expected in
        # 'transfers' or 'hardlinks'
        integrated_files = []
        if instance.data.get('transfers', False) or \
                instance.data.get('hardlinks', False):
            # could throw exception, will be caught in 'process'
            # all integration to DB is being done together lower,
            # so no rollback needed
            # todo: separate the actual integrating of the files onto its own
            #       taking just a list of transfers as inputs (potentially
            #       with copy mode flag, like hardlink/copy, etc.)
            self.log.debug("Integrating source files to destination ...")
            integrated_files = self.integrate(instance)
            self.log.debug("Integrated files {}".format(integrated_files))

        # get 'files' info for representation and all attached resources
        self.log.debug("Preparing files information ...")
        representation["files"] = self.get_files_info(
            instance,
            integrated_files
        )

        return {
            "representation": representation,
            "anatomy_data": template_data,
            # todo: avoid the need for 'published_files'?
            # backwards compatibility
            "published_files": [transfer[1] for transfer in repre["transfers"]]
        }

    def integrate(self, instance):
        """ Move the files.

            Through `instance.data["transfers"]`

            Args:
                instance: the instance to integrate
            Returns:
                list: destination full paths of integrated files
        """
        # store destinations for potential rollback and measuring sizes
        destinations = []
        transfers = list(instance.data.get("transfers", list()))
        for src, dest in transfers:
            src = os.path.normpath(src)
            dest = os.path.normpath(dest)
            if src != dest:
                dest = self.get_dest_temp_url(dest)
                self.copy_file(src, dest)
                destinations.append(dest)

        # Produce hardlinked copies
        hardlinks = instance.data.get("hardlinks", list())
        for src, dest in hardlinks:
            dest = self.get_dest_temp_url(dest)
            if not os.path.exists(dest):
                self.hardlink_file(src, dest)

            destinations.append(dest)

        return destinations

    def _create_folder_for_file(self, path):
        dirname = os.path.dirname(path)
        try:
            os.makedirs(dirname)
        except OSError as e:
            if e.errno == errno.EEXIST:
                pass
            else:
                self.log.critical("An unexpected error occurred.")
                six.reraise(*sys.exc_info())

    def copy_file(self, src, dst):
        """Copy source filepath to destination filepath

        Arguments:
            src (str): the source file which needs to be copied
            dst (str): the destination filepath

        Returns:
            None

        """
        self._create_folder_for_file(dst)
        self.log.debug("Copying file ... {} -> {}".format(src, dst))
        copyfile(src, dst)

    def hardlink_file(self, src, dst):
        """Hardlink source filepath to destination filepath.

        Note:
            Hardlink can only be produced between two files on the same
            server/disk and editing one of the two will edit both files at
            once. As such it is recommended to only make hardlinks between
            static files to ensure publishes remain safe and non-edited.

        Arguments:
            src (str): the source file which needs to be hardlinked
            dst (str): the destination filepath

        Returns:
            None
        """
        self._create_folder_for_file(dst)
        self.log.debug("Hardlinking file ... {} -> {}".format(src, dst))
        create_hard_link(src, dst)

    def _get_instance_families(self, instance):
        """Get all families of the instance"""
        # todo: move this to lib?
        family = instance.data.get("family")
        families = []
        if family:
            families.append(family)

        for _family in (instance.data.get("families") or []):
            if _family not in families:
                families.append(_family)

        return families

    def register_subset(self, instance):
        # todo: rely less on self.prepare_anatomy to create this value
        asset = instance.data.get("assetEntity") # <- from prepare_anatomy :(
        subset_name = instance.data["subset"]
        subset = io.find_one({
            "type": "subset",
            "parent": asset["_id"],
            "name": subset_name
        })

        if subset is None:
            self.log.info("Subset '%s' not found, creating ..." % subset_name)
            families = self._get_instance_families(instance)

            _id = io.insert_one({
                "schema": "openpype:subset-3.0",
                "type": "subset",
                "name": subset_name,
                "data": {
                    "families": families
                },
                "parent": asset["_id"]
            }).inserted_id

            subset = io.find_one({"_id": _id})

        # Update subset group
        self._set_subset_group(instance, subset["_id"])

        # Update families on subset.
        families = [instance.data["family"]]
        families.extend(instance.data.get("families", []))
        io.update_many(
            {"type": "subset", "_id": io.ObjectId(subset["_id"])},
            {"$set": {"data.families": families}}
        )

        return subset

    def _set_subset_group(self, instance, subset_id):
        """
            Mark subset as belonging to group in DB.

            Uses Settings > Global > Publish plugins > IntegrateAssetNew

            Args:
                instance (dict): processed instance
                subset_id (str): DB's subset _id

        """
        # Fist look into instance data
        subset_group = instance.data.get("subsetGroup")
        if not subset_group:
            subset_group = self._get_subset_group(instance)

        if subset_group:
            io.update_many({
                'type': 'subset',
                '_id': io.ObjectId(subset_id)
            }, {'$set': {'data.subsetGroup': subset_group}})

    def _get_subset_group(self, instance):
        """Look into subset group profiles set by settings.

        Attribute 'subset_grouping_profiles' is defined by OpenPype settings.
        """
        # Skip if 'subset_grouping_profiles' is empty
        if not self.subset_grouping_profiles:
            return None

        # QUESTION
        #   - is there a chance that task name is not filled in anatomy
        #       data?
        #   - should we use context task in that case?
        anatomy_data = instance.data["anatomyData"]
        task_name = None
        task_type = None
        if "task" in anatomy_data:
            task_name = anatomy_data["task"]["name"]
            task_type = anatomy_data["task"]["type"]
        filtering_criteria = {
            "families": instance.data["family"],
            "hosts": instance.context.data["hostName"],
            "tasks": task_name,
            "task_types": task_type
        }
        matching_profile = filter_profiles(
            self.subset_grouping_profiles,
            filtering_criteria
        )
        # Skip if there is not matching profile
        if not matching_profile:
            return None

        filled_template = None
        template = matching_profile["template"]
        fill_pairs = (
            ("family", filtering_criteria["families"]),
            ("task", filtering_criteria["tasks"]),
            ("host", filtering_criteria["hosts"]),
            ("subset", instance.data["subset"]),
            ("renderlayer", instance.data.get("renderlayer"))
        )
        fill_pairs = prepare_template_data(fill_pairs)

        try:
            filled_template = \
                format_template_with_optional_keys(fill_pairs, template)
        except KeyError:
            keys = []
            if fill_pairs:
                keys = fill_pairs.keys()

            msg = "Subset grouping failed. " \
                  "Only {} are expected in Settings".format(','.join(keys))
            self.log.warning(msg)

        return filled_template

    def create_version_data(self, instance):
        """Create the data collection for the version

        Args:
            instance: the current instance being published

        Returns:
            dict: the required information with instance.data as key
        """

        context = instance.context

        # create relative source path for DB
        if "source" in instance.data:
            source = instance.data["source"]
        else:
            source = context.data["currentFile"]
            anatomy = instance.context.data["anatomy"]
            source = self.get_rootless_path(anatomy, source)
        self.log.debug("Source: {}".format(source))

        version_data = {
            "families": self._get_instance_families(instance),
            "time": context.data["time"],
            "author": context.data["user"],
            "source": source,
            "comment": context.data.get("comment"),
            "machine": context.data.get("machine"),
            "fps": context.data.get(
                "fps", instance.data.get("fps")
            )
        }

        intent_value = context.data.get("intent")
        if intent_value and isinstance(intent_value, dict):
            intent_value = intent_value.get("value")

        if intent_value:
            version_data["intent"] = intent_value

        # Include optional data if present in
        optionals = [
            "frameStart", "frameEnd", "step", "handles",
            "handleEnd", "handleStart", "sourceHashes"
        ]
        for key in optionals:
            if key in instance.data:
                version_data[key] = instance.data[key]

        return version_data

    def main_family_from_instance(self, instance):
        """Returns main family of entered instance."""
        return self._get_instance_families(instance)[0]

    def get_rootless_path(self, anatomy, path):
        """  Returns, if possible, path without absolute portion from host
             (eg. 'c:\' or '/opt/..')
             This information is host dependent and shouldn't be captured.
             Example:
                 'c:/projects/MyProject1/Assets/publish...' >
                 '{root}/MyProject1/Assets...'

        Args:
                anatomy: anatomy part from instance
                path: path (absolute)
        Returns:
                path: modified path if possible, or unmodified path
                + warning logged
        """
        success, rootless_path = (
            anatomy.find_root_template_from_path(path)
        )
        if success:
            path = rootless_path
        else:
            self.log.warning((
                "Could not find root path for remapping \"{}\"."
                " This may cause issues on farm."
            ).format(path))
        return path

    def get_files_info(self, instance):
        """ Prepare 'files' portion for attached resources and main asset.
            Combining records from 'transfers' and 'hardlinks' parts from
            instance.
            All attached resources should be added, currently without
            Context info.

        Arguments:
            instance: the current instance being published
            integrated_file_sizes: dictionary of destination path (absolute)
            and its file size
        Returns:
            output_resources: array of dictionaries to be added to 'files' key
            in representation
        """
        # todo: refactor to use transfers/hardlinks of representations
        #       currently broken logic
        resources = list(instance.data.get("transfers", []))
        resources.extend(list(instance.data.get("hardlinks", [])))
        self.log.debug("get_files_info.resources:{}".format(resources))

        sites = self.compute_resource_sync_sites(instance)

        output_resources = []
        anatomy = instance.context.data["anatomy"]
        for _src, dest in resources:
            file_info = self.prepare_file_info(dest, anatomy, sites=sites)
            output_resources.append(file_info)

        return output_resources

    def get_dest_temp_url(self, dest):
        """ Enhance destination path with TMP_FILE_EXT to denote temporary
            file.
            Temporary files will be renamed after successful registration
            into DB and full copy to destination

        Arguments:
            dest: destination url of published file (absolute)
        Returns:
            dest: destination path + '.TMP_FILE_EXT'
        """
        if self.TMP_FILE_EXT and '.{}'.format(self.TMP_FILE_EXT) not in dest:
            dest += '.{}'.format(self.TMP_FILE_EXT)
        return dest

    def get_dest_final_url(self, temp_file_url):
        """Temporary destination file url to final destination file url"""
        return re.sub(r'\.{}$'.format(self.TMP_FILE_EXT), '', temp_file_url)

    def prepare_file_info(self, path, anatomy, sites):
        """ Prepare information for one file (asset or resource)

        Arguments:
            path: destination url of published file (rootless)
            size(optional): size of file in bytes
            file_hash(optional): hash of file for synchronization validation
            sites(optional): array of published locations,
                            [ {'name':'studio', 'created_dt':date} by default
                                keys expected ['studio', 'site1', 'gdrive1']
        Returns:
            rec: dictionary with filled info
        """
        file_hash = openpype.api.source_hash(path)

        # todo: Avoid this logic
        # Strip the temporary file extension from the file hash
        if self.TMP_FILE_EXT and ',{}'.format(self.TMP_FILE_EXT) in file_hash:
            file_hash = file_hash.replace(',{}'.format(self.TMP_FILE_EXT), '')

        return {
            "_id": io.ObjectId(),
            "path": self.get_rootless_path(anatomy, path),
            "size": os.path.getsize(path),
            "hash": file_hash,
            "sites": sites
        }

    def compute_resource_sync_sites(self, instance):
        """Get available resource sync sites"""
        # Sync server logic
        # TODO: Clean up sync settings
        local_site = 'studio'  # default
        remote_site = None
        always_accessible = []
        sync_project_presets = None

        system_sync_server_presets = (
            instance.context.data["system_settings"]
                                 ["modules"]
                                 ["sync_server"])
        log.debug("system_sett:: {}".format(system_sync_server_presets))

        if system_sync_server_presets["enabled"]:
            sync_project_presets = (
                instance.context.data["project_settings"]
                                     ["global"]
                                     ["sync_server"])

        if sync_project_presets and sync_project_presets["enabled"]:
            local_site, remote_site = self._get_sites(sync_project_presets)
            always_accessible = sync_project_presets["config"]. \
                get("always_accessible_on", [])

        already_attached_sites = {}
        meta = {"name": local_site, "created_dt": datetime.now()}
        sites = [meta]
        already_attached_sites[meta["name"]] = meta["created_dt"]

        if sync_project_presets and sync_project_presets["enabled"]:
            if remote_site and \
                    remote_site not in already_attached_sites.keys():
                # add remote
                meta = {"name": remote_site.strip()}
                sites.append(meta)
                already_attached_sites[meta["name"]] = None

            # add skeleton for site where it should be always synced to
            for always_on_site in always_accessible:
                if always_on_site not in already_attached_sites.keys():
                    meta = {"name": always_on_site.strip()}
                    sites.append(meta)
                    already_attached_sites[meta["name"]] = None

            # add alternative sites
            alt = self._add_alternative_sites(system_sync_server_presets,
                                              already_attached_sites)
            sites.extend(alt)

        log.debug("final sites:: {}".format(sites))

        return sites

    def _get_sites(self, sync_project_presets):
        """Returns tuple (local_site, remote_site)"""
        local_site_id = openpype.api.get_local_site_id()
        local_site = sync_project_presets["config"]. \
            get("active_site", "studio").strip()

        if local_site == 'local':
            local_site = local_site_id

        remote_site = sync_project_presets["config"].get("remote_site")

        if remote_site == 'local':
            remote_site = local_site_id

        return local_site, remote_site

    def _add_alternative_sites(self,
                               system_sync_server_presets,
                               already_attached_sites):
        """Loop through all configured sites and add alternatives.

            See SyncServerModule.handle_alternate_site
        """
        conf_sites = system_sync_server_presets.get("sites", {})

        alternative_sites = []
        for site_name, site_info in conf_sites.items():
            alt_sites = set(site_info.get("alternative_sites", []))
            already_attached_keys = list(already_attached_sites.keys())
            for added_site in already_attached_keys:
                if added_site in alt_sites:
                    if site_name in already_attached_keys:
                        continue
                    meta = {"name": site_name}
                    real_created = already_attached_sites[added_site]
                    # alt site inherits state of 'created_dt'
                    if real_created:
                        meta["created_dt"] = real_created
                    alternative_sites.append(meta)
                    already_attached_sites[meta["name"]] = real_created

        return alternative_sites

    def handle_destination_files(self, destinations, mode):
        """ Clean destination files
            Called when error happened during integrating to DB or to disk
            OR called to rename uploaded files from temporary name to final to
            highlight publishing in progress/broken
            Used to clean unwanted files

        Arguments:
            destinations (list): file paths
            mode: 'remove' - clean files,
                  'finalize' - rename files,
                               remove TMP_FILE_EXT suffix denoting temp file
        """
        if not destinations:
            return

        for file_url in destinations:
            if not os.path.exists(file_url):
                self.log.debug(
                    "File {} was not found.".format(file_url)
                )
                continue

            try:
                if mode == 'remove':
                    self.log.debug("Removing file {}".format(file_url))
                    os.remove(file_url)
                if mode == 'finalize':

                    new_name = self.get_dest_final_url(file_url)
                    if os.path.exists(new_name):
                        self.log.debug("Removing existing "
                                       "file: {}".format(new_name))
                        os.remove(new_name)

                    self.log.debug(
                        "Renaming file {} to {}".format(file_url, new_name)
                    )
                    os.rename(file_url, new_name)
            except OSError:
                self.log.error("Cannot {} file {}".format(mode, file_url),
                               exc_info=True)
                six.reraise(*sys.exc_info())
