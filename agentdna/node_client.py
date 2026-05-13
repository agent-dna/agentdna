from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union


def resolve_chain_url(
    base_url: Optional[str] = None,
    chain_url: Optional[str] = None,
    config_path: Optional[Union[str, Path]] = None,
) -> str:
    """
    Resolve the Rubix node URL from, in order:
      1. base_url (explicit)
      2. chain_url (explicit)
      3. config_path (or agentdna/config.json)['chain_url']
      4. CHAIN_URL env var
    Raises ValueError if none of those produce a URL.
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "config.json"

    cfg_chain: Optional[str] = None
    try:
        with Path(config_path).open("r", encoding="utf-8") as f:
            cfg_chain = json.load(f).get("chain_url")
    except (FileNotFoundError, json.JSONDecodeError):
        cfg_chain = None

    final_url = base_url or chain_url or cfg_chain or os.getenv("CHAIN_URL")
    if not final_url:
        raise ValueError(
            "No Rubix node URL found. Set chain_url, config.json['chain_url'], or CHAIN_URL."
        )
    return final_url.rstrip("/")


class NodeClient:
    """
    Back-compat shim — prefer ``resolve_chain_url(...)``.

    Several examples instantiate ``NodeClient(alias=...)`` just to obtain a
    base URL. This class preserves that surface (constructor + ``get_base_url``)
    so those examples keep working unchanged.
    """

    def __init__(
        self,
        alias: Optional[str] = None,  # accepted for back-compat, not used
        base_url: Optional[str] = None,
        chain_url: Optional[str] = None,
        config_path: Optional[Union[str, Path]] = None,
    ):
        self.base_url = resolve_chain_url(
            base_url=base_url,
            chain_url=chain_url,
            config_path=config_path,
        )

    def get_base_url(self) -> str:
        return self.base_url
