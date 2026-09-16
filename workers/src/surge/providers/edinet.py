"""EDINET code list: stable issuer identifiers for Japanese issuers.

The list maps the exchange securities code to an EDINET code and the Japanese
corporate number, which gives JP issuers a registry identity instead of a
name-derived one.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass

from surge.http_fetch import fetch
from surge.models import Provenance

SOURCE_ID = "edinet_code_list"
DEFAULT_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

_EDINET_CODE_COLUMN = "ＥＤＩＮＥＴコード"
_SECURITIES_CODE_COLUMN = "証券コード"
_NAME_COLUMN = "提出者名"
_CORPORATE_NUMBER_COLUMN = "提出者法人番号"


@dataclass(frozen=True)
class EdinetIssuer:
    edinet_code: str
    name: str
    corporate_number: str | None


def jpx_code_to_securities_code(local_code: str) -> str:
    """JPX publishes 4 character codes; EDINET uses the 5 character form."""

    code = local_code.strip()
    return code if len(code) == 5 else f"{code}0"


def parse_code_list(payload: bytes) -> dict[str, EdinetIssuer]:
    """Return securities code -> issuer. Rows without a securities code are skipped."""

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        text = archive.read(member).decode("cp932", errors="replace")

    lines = text.splitlines()
    # The first line is a download banner; the second line holds the headers.
    reader = csv.DictReader(lines[1:])
    mapping: dict[str, EdinetIssuer] = {}
    for row in reader:
        securities_code = (row.get(_SECURITIES_CODE_COLUMN) or "").strip()
        edinet_code = (row.get(_EDINET_CODE_COLUMN) or "").strip()
        if not securities_code or not edinet_code:
            continue
        corporate_number = (row.get(_CORPORATE_NUMBER_COLUMN) or "").strip() or None
        mapping[securities_code] = EdinetIssuer(
            edinet_code=edinet_code,
            name=(row.get(_NAME_COLUMN) or "").strip(),
            corporate_number=corporate_number,
        )
    return mapping


class EdinetCodeList:
    provider_id = SOURCE_ID

    def __init__(self, url: str = DEFAULT_URL, *, user_agent: str | None = None) -> None:
        self._url = url
        self._user_agent = user_agent

    def fetch_map(self) -> tuple[dict[str, EdinetIssuer], Provenance]:
        kwargs = {"user_agent": self._user_agent} if self._user_agent else {}
        response = fetch(self._url, **kwargs)
        mapping = parse_code_list(response.body)

        provenance = Provenance(
            source_id=SOURCE_ID,
            endpoint=self._url,
            requested_at=response.requested_at,
            received_at=response.received_at,
            http_status=response.status,
            bytes=response.bytes,
            content_sha256=response.sha256,
            item_count=len(mapping),
            observed_at=response.received_at,
            available_at=response.received_at,
        )
        return mapping, provenance
