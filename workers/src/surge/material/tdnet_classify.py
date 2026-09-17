"""Typing a TDnet disclosure from its title alone.

This exists because the body is out of reach. TDnet prohibits reproducing its
documents, so all a discovery pass has is the headline - and a headline is
enough to say *what kind of thing* was disclosed, which is what routes an event
to the right verification source and the right Phase 5 features.

Three things it deliberately does **not** do.

**It never excludes.** An unrecognised title becomes ``OTHER`` and still
produces a material candidate. Relevance is decided in Phase 5 from structural
signals, and a lexical filter that silently dropped disclosures would be exactly
the keyword exclusion the project forbids.

**It never claims more confidence than text deserves.** Every classification is
PROVISIONAL, in the same three-step vocabulary the identity model uses. A title
is evidence about a document, not the document.

**It does not treat a substring as a match.** 異動 means one thing after 子会社
and another after 代表取締役; 予想の修正 is earnings or dividends depending on
what precedes it; 自己株式 is followed by four different verbs that are four
different corporate actions. Rules are ordered most-specific-first and anchored
on the distinguishing context, and the suite checks them against 300 real titles
rather than against invented ones.

Patterns were derived from live data on 2026-09-17, not from imagination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

CLASSIFIER_VERSION = "tdnet-title-1.0.0"


class DisclosureType(StrEnum):
    EARNINGS_RESULT = "EARNINGS_RESULT"
    EARNINGS_FORECAST_REVISION = "EARNINGS_FORECAST_REVISION"
    EARNINGS_FORECAST = "EARNINGS_FORECAST"
    DIVIDEND_FORECAST_REVISION = "DIVIDEND_FORECAST_REVISION"
    DIVIDEND = "DIVIDEND"
    BUYBACK = "BUYBACK"
    TREASURY_DISPOSAL = "TREASURY_DISPOSAL"
    TREASURY_CANCELLATION = "TREASURY_CANCELLATION"
    TENDER_OFFER = "TENDER_OFFER"
    SHARE_SPLIT = "SHARE_SPLIT"
    SHARE_CONSOLIDATION = "SHARE_CONSOLIDATION"
    EQUITY_ISSUANCE = "EQUITY_ISSUANCE"
    STOCK_OPTION = "STOCK_OPTION"
    MA_SUBSIDIARY = "MA_SUBSIDIARY"
    ALLIANCE = "ALLIANCE"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    MAJOR_SHAREHOLDER_CHANGE = "MAJOR_SHAREHOLDER_CHANGE"
    LISTING_CHANGE = "LISTING_CHANGE"
    MONTHLY_UPDATE = "MONTHLY_UPDATE"
    ETF_DAILY = "ETF_DAILY"
    ETF_DISTRIBUTION = "ETF_DISTRIBUTION"
    PRODUCT_APPROVAL = "PRODUCT_APPROVAL"
    CONTRACT = "CONTRACT"
    FINANCING = "FINANCING"
    EXTRAORDINARY_GAIN = "EXTRAORDINARY_GAIN"
    EXTRAORDINARY_LOSS = "EXTRAORDINARY_LOSS"
    INCIDENT = "INCIDENT"
    ASSET_TRANSACTION = "ASSET_TRANSACTION"
    SHAREHOLDER_MEETING = "SHAREHOLDER_MEETING"
    AUDITOR_CHANGE = "AUDITOR_CHANGE"
    LEGAL = "LEGAL"
    EMPLOYEE_SHARE_PLAN = "EMPLOYEE_SHARE_PLAN"
    INVESTIGATION = "INVESTIGATION"
    GOVERNANCE = "GOVERNANCE"
    SHAREHOLDER_PERK = "SHAREHOLDER_PERK"
    EARNINGS_MATERIAL = "EARNINGS_MATERIAL"
    OTHER = "OTHER"


#: Types whose content is routine and near-identical every session. Recorded as
#: a property of the type rather than acted on here: Phase 5 may want to weight
#: them down, and something has to be able to ask "how much of today was this?".
ROUTINE_TYPES: frozenset[DisclosureType] = frozenset(
    {
        DisclosureType.ETF_DAILY,
        DisclosureType.ETF_DISTRIBUTION,
        DisclosureType.MONTHLY_UPDATE,
    }
)


@dataclass(frozen=True)
class Classification:
    disclosure_type: DisclosureType
    matched_pattern: str | None
    #: A correction to a previous disclosure. A flag rather than a type, because
    #: a corrected earnings release is still an earnings release - and knowing
    #: which one was corrected matters more than knowing that something was.
    is_correction: bool = False
    #: 開示事項の経過 / 経過開示 / 開示事項の変更: an update to a disclosure already
    #: made. Also a flag, for the same reason.
    is_progress_update: bool = False
    classifier_version: str = CLASSIFIER_VERSION

    @property
    def classification_confidence(self) -> str:
        """Always PROVISIONAL. A title is evidence about a document, not one.

        Named in full because this evidence blob sits beside the *link*
        confidence in ``material.entity_relations`` and ``event_sources``, and
        the two answer different questions: this one is how sure we are of the
        disclosure *type*, the other is how sure we are which security it is
        about. A bare ``confidence`` key next to ``mapping_confidence`` reads as
        a second opinion on the same thing, and it is not one.
        """

        return "PROVISIONAL"

    @property
    def is_routine(self) -> bool:
        return self.disclosure_type in ROUTINE_TYPES

    def as_evidence(self) -> dict:
        return {
            "classifier_version": self.classifier_version,
            "disclosure_type": self.disclosure_type.value,
            "matched_pattern": self.matched_pattern,
            "is_correction": self.is_correction,
            "is_progress_update": self.is_progress_update,
            "classification_confidence": self.classification_confidence,
        }


_CORRECTION = re.compile(r"[（(]\s*訂正\s*[）)]")
_PROGRESS = re.compile(r"[（(]\s*(?:開示事項の経過|経過開示|開示事項の変更)\s*[）)]")

# Ordered most-specific first. The order is the algorithm: several of these
# would match the same title, and the first is the one that says most.
_RULES: tuple[tuple[DisclosureType, str], ...] = (
    # --- ETF and fund routine. Very high volume; matched early so they cannot
    # be captured by the generic 分配金 or 上場 rules below.
    (DisclosureType.ETF_DAILY, r"日々の開示事項"),
    (DisclosureType.ETF_DISTRIBUTION, r"(?:収益分配金|分配金(?:見込|予想))"),
    # --- treasury stock: four verbs, four different corporate actions.
    # 株式 and 処分 are not always adjacent - 「譲渡制限付株式報酬としての自己株式処分」
    # has no の - so the gap is allowed for rather than assumed away.
    (DisclosureType.TREASURY_CANCELLATION, r"自己株式.{0,6}消却"),
    (DisclosureType.TREASURY_DISPOSAL, r"自己株式.{0,6}処分"),
    (DisclosureType.BUYBACK, r"自己株式.{0,8}(?:取得|買付|買受)"),
    # --- takeovers before anything else about shares.
    (DisclosureType.TENDER_OFFER, r"公開買付|ＴＯＢ|TOB"),
    # --- forecasts. 配当 first: both end in 予想の修正, and checking 業績 first
    # would capture 「配当予想の修正」 whenever the title also mentioned results.
    (DisclosureType.DIVIDEND_FORECAST_REVISION, r"配当予想.{0,4}(?:修正|変更)"),
    (DisclosureType.EARNINGS_FORECAST_REVISION, r"業績予想.{0,6}(?:修正|変更)"),
    # A first publication is new guidance rather than a change to it, and the
    # two are kept apart because "the number moved" and "there is now a number"
    # are different signals.
    (DisclosureType.EARNINGS_FORECAST, r"業績予想.{0,6}(?:公表|開示|発表)"),
    # 配当金受領 is money arriving from a subsidiary, not a distribution to
    # shareholders, so the pattern anchors on the qualifiers that mark the
    # latter rather than on 配当 alone.
    (DisclosureType.DIVIDEND, r"剰余金の配当|(?:中間|期末|普通|特別|記念)配当|配当予想"),
    # --- earnings. Only 短信 is the release; 説明資料 and 説明会 are supporting
    # material that follows it and should not be typed as the result itself.
    (DisclosureType.EARNINGS_RESULT, r"決算短信"),
    (DisclosureType.EARNINGS_MATERIAL, r"決算(?:説明|補足|参考|概要|情報)|業績概要|決算を終えて"),
    # --- extraordinary items. Gains and losses are separated because they are
    # opposite signals that share a grammar, and an asset sale that books a gain
    # is typed by the gain: that is the number the market reacts to.
    (DisclosureType.EXTRAORDINARY_GAIN, r"特別利益|売却益|補助金収入|(?:営業外)?収益.{0,6}計上"),
    (DisclosureType.EXTRAORDINARY_LOSS, r"特別損失|減損損失|評価損|売却損"),
    (DisclosureType.INCIDENT, r"火災|事故|不適切|不正行為|継続企業の前提|債務超過|お詫び"),
    # --- share count.
    (DisclosureType.SHARE_CONSOLIDATION, r"株式併合"),
    (DisclosureType.SHARE_SPLIT, r"株式分割"),
    (DisclosureType.EMPLOYEE_SHARE_PLAN, r"ＥＳＯＰ|ESOP|株式付与|譲渡制限付株式(?:制度|報酬制度)|株式報酬制度"),
    (DisclosureType.STOCK_OPTION, r"新株予約権|ストック・?オプション"),
    (DisclosureType.EQUITY_ISSUANCE, r"第三者割当|募集株式|新株式発行|公募増資|投資口の発行|資本金の額の減少"),
    # --- ownership and control. 異動 alone means nothing; the noun before it
    # decides whether this is M&A, a board change, an auditor change or a
    # shareholder change, and each is checked against its own context.
    (DisclosureType.AUDITOR_CHANGE, r"公認会計士等の異動|会計監査人"),
    (DisclosureType.MA_SUBSIDIARY,
     r"子会社.{0,12}(?:異動|取得|譲渡|設立|解散|清算|化|商号変更|移転)"
     r"|(?:持分法適用)?関連会社.{0,10}異動|株式取得|合併|会社分割|株式交換|事業譲渡"),
    (DisclosureType.MAJOR_SHAREHOLDER_CHANGE, r"(?:主要株主|支配株主|筆頭株主).{0,10}(?:異動|に関する事項)"),
    (DisclosureType.MANAGEMENT_CHANGE,
     r"(?:代表取締役|取締役|執行役員|役員|人事|使用人).{0,12}(?:異動|変更|選任|辞任|退任|新設|導入)"
     r"|役員報酬|指名報酬委員会"),
    # --- everything else, roughly by how much it tends to move a price.
    (DisclosureType.ALLIANCE, r"(?:資本)?業務提携|事業提携|協業|覚書|ＭＯＵ|MOU"),
    (DisclosureType.INVESTIGATION, r"(?:特別調査委員会|第三者委員会|調査委員会).{0,12}(?:設置|報告|公表)"),
    (DisclosureType.LEGAL, r"独占禁止法|訴訟|判決|更生計画|上告|court|課徴金"),
    (DisclosureType.PRODUCT_APPROVAL, r"(?:製造販売)?承認(?:取得|申請)|薬事承認|一部変更承認|認定に関する"),
    (DisclosureType.CONTRACT, r"契約.{0,6}締結|受注|販売許諾|ライセンス契約|特許(?:出願|取得)"),
    (DisclosureType.FINANCING, r"資金の借入|借入の決定|コミットメントライン|社債の発行|投資法人債"),
    (DisclosureType.ASSET_TRANSACTION,
     r"(?:販売用)?不動産の(?:売却|取得|譲渡)|固定資産の(?:取得|譲渡|売却)|事業の(?:取得|譲受)"),
    (DisclosureType.LISTING_CHANGE,
     r"上場(?:承認|廃止|申請|維持基準)|上場に伴う|市場(?:変更|区分の?変更)|整理銘柄|監理銘柄|重複上場|株式上場"),
    (DisclosureType.SHAREHOLDER_MEETING, r"株主総会"),
    (DisclosureType.SHAREHOLDER_PERK, r"(?:株主|投資主)優待"),
    (DisclosureType.MONTHLY_UPDATE,
     r"月次|月度|売上(?:概況|報告|進捗)|営業報告|売上収益報告|ＫＰＩ|KPI|投資資産残高"),
    (DisclosureType.GOVERNANCE, r"資本コスト|事業計画及び成長可能性|コーポレートガバナンス|支配株主等に関する"),
)

_COMPILED: tuple[tuple[DisclosureType, re.Pattern[str]], ...] = tuple(
    (disclosure_type, re.compile(pattern)) for disclosure_type, pattern in _RULES
)


def classify(title: str | None) -> Classification:
    """Type one disclosure title.

    The flags are read first and then stripped, so 「（訂正）決算短信」 types as an
    earnings release that happens to be a correction rather than as a correction
    of unknown subject.
    """

    text = (title or "").strip()
    if not text:
        return Classification(disclosure_type=DisclosureType.OTHER, matched_pattern=None)

    is_correction = bool(_CORRECTION.search(text))
    is_progress = bool(_PROGRESS.search(text))
    body = _PROGRESS.sub("", _CORRECTION.sub("", text)).strip()

    for disclosure_type, pattern in _COMPILED:
        found = pattern.search(body)
        if found:
            return Classification(
                disclosure_type=disclosure_type,
                matched_pattern=found.group(0),
                is_correction=is_correction,
                is_progress_update=is_progress,
            )

    return Classification(
        disclosure_type=DisclosureType.OTHER,
        matched_pattern=None,
        is_correction=is_correction,
        is_progress_update=is_progress,
    )


def summarise(classifications) -> dict[str, int]:
    counts: dict[str, int] = {}
    for classification in classifications:
        key = classification.disclosure_type.value
        counts[key] = counts.get(key, 0) + 1
    counts["_corrections"] = sum(1 for c in classifications if c.is_correction)
    counts["_progress_updates"] = sum(1 for c in classifications if c.is_progress_update)
    counts["_routine"] = sum(1 for c in classifications if c.is_routine)
    return counts
