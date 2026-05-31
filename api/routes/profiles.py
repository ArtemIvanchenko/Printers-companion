from fastapi import APIRouter

from profiles.m350.profile import get_profile


router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("")
def list_profiles() -> list[dict]:
    profile = get_profile()
    return [
        {
            "profile_id": profile.profile_id,
            "vendor": profile.vendor,
            "model_family": profile.model_family,
            "legacy_names": profile.legacy_names,
            "version": profile.version,
        }
    ]


@router.get("/{profile_id}")
def get_profile_detail(profile_id: str) -> dict:
    profile = get_profile()
    return {
        "profile_id": profile.profile_id,
        "vendor": profile.vendor,
        "model_family": profile.model_family,
        "legacy_names": profile.legacy_names,
        "version": profile.version,
        "file_families": [family.__dict__ for family in profile.file_families],
    }


@router.get("/{profile_id}/signals")
def get_profile_signals(profile_id: str) -> dict:
    return get_profile().signal_mappings


@router.post("/{profile_id}/signals")
def map_profile_signal(profile_id: str, payload: dict) -> dict:
    return {
        "profile_id": profile_id,
        "status": "accepted_for_review",
        "message": "Signal mapping changes are versioned and affected sessions should be marked for re-analysis.",
        "payload": payload,
    }

