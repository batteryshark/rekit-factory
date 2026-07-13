"""A durable, supervised dark factory for reverse engineering."""

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.campaign_controller import (
    CampaignController, CampaignHandoff, CampaignSnapshot, InvestigationEpochRunner,
)
from rekit_factory.models import ModelProfile, WorkerReport

__all__ = [
    "CampaignController", "CampaignHandoff", "CampaignSnapshot",
    "InvestigationController", "InvestigationEpochRunner", "ModelProfile", "RunRequest",
    "WorkerReport",
]
__version__ = "0.2.0"
