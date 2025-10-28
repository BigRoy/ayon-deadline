from dataclasses import dataclass, field, asdict
from collections import defaultdict

import pyblish.api

from ayon_core.pipeline.publish import AYONPyblishPluginMixin
from ayon_deadline import abstract_submit_deadline

# Constants for storing some cached data in context/instance data
prefix = "__FusionSubmitDeadline"
FARM_SAVER_INSTANCES_KEY = f"{prefix}_saver_instances"
FARM_SAVER_INSTANCES_GROUPS_KEY = f"{prefix}_saver_instances_groups"
HAS_RUN_KEY: str = f"{prefix}_has_run"


@dataclass
class FusionPluginInfo:
    FlowFile: str = field(default=None)   # Input
    Version: str = field(default=None)    # Mandatory for Deadline

    # Render in high quality
    HighQuality: bool = field(default=True)
    # Whether saver output should be checked after rendering
    # is complete
    CheckOutput: bool = field(default=True)
    # Proxy: higher numbers smaller images for faster test renders
    # 1 = no proxy quality
    Proxy: int = field(default=1)
    # Tools: Comma separated list of tools to render
    # saver1,saver2,saver3
    # NOTE: This requires a customized Deadline Fusion plug-in that supports
    #  filtering which saver tools are enabled in the Deadline plug-in.
    #  A customization currently existing at Colorbleed animation studios.
    Tools: str = field(default=None)


class FusionSubmitDeadline(abstract_submit_deadline.AbstractSubmitDeadline,
                           AYONPyblishPluginMixin):
    """Submit current Comp to Deadline

    Renders are submitted to a Deadline Web Service as
    supplied via settings key "DEADLINE_REST_URL".

    """
    label = "Submit Fusion to Deadline"
    order = pyblish.api.IntegratorOrder
    hosts = ["fusion"]
    families = ["render", "image"]
    targets = ["local"]
    settings_category = "deadline"

    # presets
    plugin = None

    def process(self, instance):
        if not instance.data.get("farm"):
            self.log.debug("Render on farm is disabled. "
                           "Skipping deadline submission.")
            return

        # We are submitting a farm job not per instance - but once per Fusion
        # comp's saver frame range. This is a hack to avoid submitting multiple
        # jobs for each saver separately which would be much slower.
        # As such this instance may have already 'run' (been submitted) due to
        # being part of a frame range group instance that was already
        # processed.
        if instance.data.get(HAS_RUN_KEY, False):
            return

        saver_instances_by_frame_range = self.get_farm_savers_by_frame_range(
            instance.context
        )
        frame_range = self.get_instance_render_frame_range(instance)
        saver_instances = saver_instances_by_frame_range[frame_range]
        instance.data[FARM_SAVER_INSTANCES_KEY] = saver_instances

        super().process(instance)

        # Store the response for dependent job submission plug-ins for all
        # the instances
        transfer_keys = ["deadlineSubmissionJob", "deadline"]
        for saver_instance in saver_instances:
            for key in transfer_keys:
                saver_instance.data[key] = instance.data[key]
            saver_instance.data[HAS_RUN_KEY] = True

    def get_farm_savers_by_frame_range(
        self,
        context
    ) -> "dict[tuple[int, int], list[pyblish.api.Instance]]":
        # Use cached value
        if FARM_SAVER_INSTANCES_GROUPS_KEY in context.data:
            return context.data[FARM_SAVER_INSTANCES_GROUPS_KEY]

        # Collect all saver instances in context that are to be rendered
        saver_instances = []
        for inst in context:
            if inst.data["productType"] not in {"image", "render"}:
                # Allow only saver family instances
                continue

            if not inst.data.get("publish", True):
                # Skip inactive instances
                continue

            if not inst.data.get("farm"):
                # Consider only farm instances
                continue

            self.log.debug(f"Found farm instance: {inst.data['name']}")
            saver_instances.append(inst)

        if not saver_instances:
            raise RuntimeError("No instances found for Deadline submission")

        # Group all saver instances by frame range, we will then submit
        # to Deadline one job per frame range group so that we render unique
        # frame ranges per instance.
        saver_instances_by_frame_range = defaultdict(list)
        for saver_instance in saver_instances:
            frame_range = self.get_instance_render_frame_range(saver_instance)
            saver_instances_by_frame_range[frame_range].append(saver_instance)

        saver_instances_by_frame_range = dict(saver_instances_by_frame_range)
        context.data[FARM_SAVER_INSTANCES_GROUPS_KEY] = (
            saver_instances_by_frame_range
        )

        # Debug log the collected groups
        for frame_range, saver_instances in saver_instances_by_frame_range.items():
            saver_tool_names: list[str] = sorted(
                inst.data["tool"].Name for inst in saver_instances
            )
            self.log.debug(
                f"Frame range group {frame_range} has {len(saver_instances)}" 
                f" savers: {', '.join(saver_tool_names)}"
            )

        return saver_instances_by_frame_range

    def get_instance_render_frame_range(
        self,
        instance: pyblish.api.Instance
    ) -> tuple[int, int]:
        """Return render frame range of the given instance"""
        start = instance.data["frameStartHandle"]
        end = instance.data["frameEndHandle"]
        return start, end

    def get_job_info(self, job_info=None, **kwargs):
        instance = self._instance

        # Deadline requires integers in frame range
        job_info.Plugin = self.plugin or "Fusion"
        # already collected explicit values for rendered Frames
        if not job_info.Frames:
            job_info.Frames = "{start}-{end}".format(
                start=int(instance.data["frameStartHandle"]),
                end=int(instance.data["frameEndHandle"])
            )

        # We override the default behavior of AbstractSubmitDeadline here to
        # include the output directory and output filename for each individual
        # saver instance, instead of only the current instance, because we're
        # submitting one job for multiple savers
        saver_instances = instance.data[FARM_SAVER_INSTANCES_KEY]
        for saver_instance in saver_instances:
            if saver_instance is instance:
                continue

            self._append_job_output_paths(instance, job_info)

        return job_info

    def get_plugin_info(self):
        instance = self._instance
        saver_instances = instance.data[FARM_SAVER_INSTANCES_KEY]

        plugin_info = FusionPluginInfo(
            FlowFile=self.scene_path,
            Version=str(instance.data["app_version"]),
            Tools=",".join(inst.data["tool"].Name for inst in saver_instances)
        )
        plugin_payload: dict = asdict(plugin_info)
        return plugin_payload
