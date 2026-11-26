"""bookdoe provider for Music Assistant. Setup and caveats: docs/MUSIC_ASSISTANT.md."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import BookdoeProvider, get_config_entries

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__all__ = ["get_config_entries", "setup"]


async def setup(mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig) -> ProviderInstanceType:
    """Create the provider instance."""
    return BookdoeProvider(mass, manifest, config)
