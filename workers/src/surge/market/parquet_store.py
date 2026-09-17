"""Canonical series on Parquet, in whatever object store is configured.

Postgres holds state, indexes and decisions; the bulk price history lives as
Parquet in object storage, which is what keeps the recurring cost at zero. A
year of the full universe is roughly a couple of hundred megabytes compressed,
well inside R2's free allowance, and the same code writes to a local directory
during development.

Two properties are deliberate:

*Partitioned AND content addressed.* The path says what the file is about -
provider, market, dataset, date - so a reader can find a day without a catalogue.
The filename is the digest of the bytes, so writing the same day twice lands on
the same object and writing a CORRECTED day lands beside the old one rather than
over it. Nothing is ever overwritten, and the manifest records which digest was
current when.

*Decimal, not float.* Prices are exact quantities and a 3,000 JPY threshold is an
exact comparison. Round-tripping them through binary floating point would make
the boundary case depend on the storage format.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from surge.market.models import CanonicalBar, PriceBasis
from surge.storage.base import ObjectStore, StoredObject, sha256_hex

PARQUET_COMPRESSION = "zstd"

# 28 digits with 8 decimals: twenty integer digits is far more than any share
# price or share count needs, and the scale survives a JPY price to the sen.
_PRICE_TYPE_ARGS = (28, 8)


class ParquetUnavailable(RuntimeError):
    """pyarrow is not installed. The caller decides whether that is fatal."""


def _pyarrow():
    try:
        import pyarrow as pa  # noqa: PLC0415 - optional dependency
        import pyarrow.parquet as pq  # noqa: PLC0415

        return pa, pq
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ParquetUnavailable(
            "pyarrow is required to read or write Parquet; install the 'parquet' extra"
        ) from exc


def bar_schema():
    """The on-disk schema, stated explicitly so it cannot drift by inference."""

    pa, _ = _pyarrow()
    decimal = pa.decimal128(*_PRICE_TYPE_ARGS)
    return pa.schema(
        [
            pa.field("provider_id", pa.string(), nullable=False),
            pa.field("dataset_key", pa.string(), nullable=False),
            pa.field("market_code", pa.string(), nullable=False),
            pa.field("native_symbol", pa.string(), nullable=False),
            pa.field("trade_date", pa.date32(), nullable=False),
            pa.field("currency", pa.string(), nullable=False),
            pa.field("open", decimal),
            pa.field("high", decimal),
            pa.field("low", decimal),
            pa.field("close", decimal),
            pa.field("volume", decimal),
            pa.field("turnover", decimal),
            pa.field("open_basis", pa.string(), nullable=False),
            pa.field("high_basis", pa.string(), nullable=False),
            pa.field("low_basis", pa.string(), nullable=False),
            pa.field("close_basis", pa.string(), nullable=False),
            pa.field("volume_basis", pa.string(), nullable=False),
            pa.field("exchange_code", pa.string()),
            pa.field("provider_security_id", pa.string()),
            pa.field("source_data_version", pa.string()),
            pa.field("identity_version", pa.string()),
            pa.field("source_timestamp", pa.timestamp("us", tz="UTC")),
            pa.field("observed_at", pa.timestamp("us", tz="UTC")),
            pa.field("available_at", pa.timestamp("us", tz="UTC")),
            pa.field("availability_basis", pa.string(), nullable=False),
            pa.field("raw_object_key", pa.string()),
        ]
    )


def partition_prefix(provider_id: str, market_code: str, dataset_key: str, trade_date: date) -> str:
    return (
        f"market_data/provider={provider_id}/market={market_code}/dataset={dataset_key}"
        f"/year={trade_date.year:04d}/month={trade_date.month:02d}/date={trade_date.isoformat()}"
    )


def _quantize(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal(1).scaleb(-_PRICE_TYPE_ARGS[1]))


def bars_to_table(bars: list[CanonicalBar]):
    pa, _ = _pyarrow()
    schema = bar_schema()

    columns: dict[str, list] = {field.name: [] for field in schema}
    for bar in bars:
        columns["provider_id"].append(bar.provider_id)
        columns["dataset_key"].append(bar.dataset_key)
        columns["market_code"].append(bar.market_code)
        columns["native_symbol"].append(bar.native_symbol)
        columns["trade_date"].append(bar.trade_date)
        columns["currency"].append(bar.currency)
        for name in ("open", "high", "low", "close", "volume", "turnover"):
            columns[name].append(_quantize(getattr(bar, name)))
        for name in ("open_basis", "high_basis", "low_basis", "close_basis", "volume_basis"):
            columns[name].append(str(getattr(bar, name)))
        columns["exchange_code"].append(bar.exchange_code)
        columns["provider_security_id"].append(bar.provider_security_id)
        columns["source_data_version"].append(bar.source_data_version)
        columns["identity_version"].append(bar.identity_version)
        columns["source_timestamp"].append(bar.source_timestamp)
        columns["observed_at"].append(bar.observed_at)
        columns["available_at"].append(bar.available_at)
        columns["availability_basis"].append(str(bar.availability_basis))
        columns["raw_object_key"].append(bar.raw_object_key)

    return pa.Table.from_pydict(columns, schema=schema)


def table_to_bars(table) -> list[CanonicalBar]:
    from surge.licensing import AvailabilityBasis  # noqa: PLC0415 - avoid a cycle at import time

    rows = table.to_pylist()
    bars: list[CanonicalBar] = []
    for row in rows:
        bars.append(
            CanonicalBar(
                provider_id=row["provider_id"],
                dataset_key=row["dataset_key"],
                market_code=row["market_code"],
                native_symbol=row["native_symbol"],
                trade_date=row["trade_date"],
                currency=row["currency"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                turnover=row["turnover"],
                open_basis=PriceBasis(row["open_basis"]),
                high_basis=PriceBasis(row["high_basis"]),
                low_basis=PriceBasis(row["low_basis"]),
                close_basis=PriceBasis(row["close_basis"]),
                volume_basis=PriceBasis(row["volume_basis"]),
                exchange_code=row["exchange_code"],
                provider_security_id=row["provider_security_id"],
                source_data_version=row["source_data_version"],
                identity_version=row["identity_version"],
                source_timestamp=row["source_timestamp"],
                observed_at=row["observed_at"],
                available_at=row["available_at"],
                availability_basis=AvailabilityBasis(row["availability_basis"]),
                raw_object_key=row["raw_object_key"],
            )
        )
    return bars


def serialise_bars(bars: list[CanonicalBar]) -> bytes:
    pa, pq = _pyarrow()
    import io  # noqa: PLC0415

    table = bars_to_table(bars)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression=PARQUET_COMPRESSION, version="2.6")
    return sink.getvalue()


def deserialise_bars(payload: bytes) -> list[CanonicalBar]:
    _, pq = _pyarrow()
    import io  # noqa: PLC0415

    return table_to_bars(pq.read_table(io.BytesIO(payload)))


@dataclass(frozen=True)
class WrittenPartition:
    key: str
    trade_date: date
    rows: int
    bytes: int
    sha256: str
    created: bool


class ParquetSeriesStore:
    """Writes and reads day partitions of canonical bars."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @property
    def store(self) -> ObjectStore:
        return self._store

    def write_day(
        self, bars: list[CanonicalBar], *, provider_id: str, market_code: str, dataset_key: str, trade_date: date
    ) -> WrittenPartition:
        """Write one trading day. Same bytes twice is a no-op, not a second file."""

        if not bars:
            raise ValueError("refusing to write an empty partition; an empty day is a finding, not a file")
        wrong_day = [b for b in bars if b.trade_date != trade_date]
        if wrong_day:
            raise ValueError(
                f"{len(wrong_day)} bar(s) are not dated {trade_date}; a day partition holds one day"
            )

        payload = serialise_bars(sorted(bars, key=lambda b: b.native_symbol))
        digest = sha256_hex(payload)
        key = f"{partition_prefix(provider_id, market_code, dataset_key, trade_date)}/{digest}.parquet"
        stored: StoredObject = self._store.put_immutable(key, payload, "application/vnd.apache.parquet")
        return WrittenPartition(
            key=stored.key,
            trade_date=trade_date,
            rows=len(bars),
            bytes=stored.bytes,
            sha256=digest,
            created=stored.created,
        )

    def read_day(
        self, *, provider_id: str, market_code: str, dataset_key: str, trade_date: date
    ) -> list[CanonicalBar]:
        """Read a day, taking the newest partition when a correction was written.

        Corrections land beside the original rather than over it, so a day can
        legitimately hold more than one file. Newest wins for a normal read;
        the older bytes remain for anyone asking what we used to think.
        """

        keys = self.list_day(
            provider_id=provider_id, market_code=market_code, dataset_key=dataset_key, trade_date=trade_date
        )
        if not keys:
            return []
        return deserialise_bars(self._store.get(keys[-1]))

    def list_day(
        self, *, provider_id: str, market_code: str, dataset_key: str, trade_date: date
    ) -> list[str]:
        prefix = partition_prefix(provider_id, market_code, dataset_key, trade_date)
        return sorted(self._store.list(prefix + "/"))

    def read_range(
        self,
        *,
        provider_id: str,
        market_code: str,
        dataset_key: str,
        start: date,
        end: date,
        symbols: set[str] | None = None,
    ) -> list[CanonicalBar]:
        """Every bar in a date range, optionally narrowed to some symbols."""

        prefix = f"market_data/provider={provider_id}/market={market_code}/dataset={dataset_key}/"
        bars: list[CanonicalBar] = []
        seen_days: dict[date, str] = {}

        for key in sorted(self._store.list(prefix)):
            day = _date_from_key(key)
            if day is None or day < start or day > end:
                continue
            # newest partition per day; sorted order makes the last one win
            seen_days[day] = key

        for _day, key in sorted(seen_days.items()):
            for bar in deserialise_bars(self._store.get(key)):
                if symbols is None or bar.native_symbol in symbols:
                    bars.append(bar)
        return bars


def _date_from_key(key: str) -> date | None:
    for part in key.split("/"):
        if part.startswith("date="):
            try:
                return date.fromisoformat(part.removeprefix("date="))
            except ValueError:
                return None
    return None
