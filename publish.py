"""publish.py — HMall(현대홈쇼핑) 편성표를 Notion 페이지로 매일 발행.

이 스크립트는 GitHub Actions 에서 매일 KST 10:00 (UTC 01:00) 에 실행된다.
의존성 최소화를 위해 단일 파일로 작성됐고, 외부 라이브러리는
`httpx` (HMall API 호출) 와 `notion-client` (Notion SDK) 두 개만 쓴다.

흐름:
  1. 환경변수 (NOTION_TOKEN, NOTION_PAGE_ID, DAYS_AHEAD) 검증
  2. KST 기준 오늘 부터 N일치 날짜 산출
  3. 각 날짜별 HMall API GET → broadItemList 추출 (실패 시 그 날만 빈 리스트)
  4. 필요한 필드만 dataclass `Item` 으로 정규화
  5. (channel_code, started_at, ended_at) 키로 슬롯 그룹핑
  6. Notion 블록 트리 빌드 (heading_1 / quote / heading_2 / heading_3 / bullets)
  7. 대상 페이지의 기존 자식 블록 삭제 → 새 블록 청크 단위로 append (재시도 3회)
  8. stdout 으로 결과 보고

실패 정책:
  - 1일치 fetch 실패: 경고 로깅 후 다른 날짜 계속, "5/7 일 성공" 표기
  - 전체 실패: 페이지를 "데이터 수집 실패" 안내로 교체하고 exit 1
  - Notion API 실패: 지수 백오프로 3회 재시도, 최종 실패면 exit 1
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import httpx
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError, HTTPResponseError, RequestTimeoutError

# ----------------------------------------------------------------------------
# 상수 / 설정
# ----------------------------------------------------------------------------

KST = ZoneInfo("Asia/Seoul")

# HMall 편성표 API — 단계 1 분석에서 확인된 비로그인 접근 가능 엔드포인트
HMALL_API_URL = "https://www.hmall.com/api/hf/dp/v1/main-tv-new/tv-list"
HMALL_OK_RESP_CODE = "dp.clt.00001"

# 단계 1 분석에서 권장된 헤더 세트 (쿠키 없이 200 OK 확인됨)
HMALL_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.hmall.com/md/dpl/index?mainDispSeq=2&brodType=all",
}

# 채널 코드 → 표시명 매핑 (단계 1 분석)
CHANNEL_DISPLAY: dict[str, str] = {
    "mtv": "TV쇼핑",
    "dtv": "TV+샵",
    "etv": "쇼라",
}

# 요일 한글 약어 (월=0 .. 일=6)
WEEKDAY_KR: list[str] = ["월", "화", "수", "목", "금", "토", "일"]

# Notion blocks.children.append 는 한 번에 최대 100개
NOTION_APPEND_CHUNK = 100

# 로깅 (GitHub Actions 콘솔용 단순 포맷)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("publish")


# ----------------------------------------------------------------------------
# 데이터 모델 (slim dataclass — Pydantic 미사용)
# ----------------------------------------------------------------------------


@dataclass
class Item:
    """슬롯 내 상품 1개 — Notion 렌더링에 필요한 최소 필드만."""

    slitm_cd: str
    slitm_nm: str
    brnd_nm: Optional[str]
    channel_code: str  # "mtv" / "dtv" / "etv"
    started_at: datetime  # KST tz-aware
    ended_at: datetime    # KST tz-aware
    bbprc: Optional[int]
    sell_prc: Optional[int]
    dc_rate: Optional[int]
    live_yn: bool
    exposure_order: int

    @property
    def detail_url(self) -> str:
        return f"https://www.hmall.com/md/pda/itemPtc?slitmCd={self.slitm_cd}"

    @property
    def slot_key(self) -> tuple[str, str, str]:
        """슬롯 그룹핑 키 — channel + 시작/종료(ISO 문자열)."""
        return (
            self.channel_code,
            self.started_at.isoformat(),
            self.ended_at.isoformat(),
        )


@dataclass
class FetchResult:
    """1일치 fetch 결과."""

    brod_dt: date
    items: list[Item] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


# ----------------------------------------------------------------------------
# 환경변수 / 날짜 헬퍼
# ----------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """필수 env 가 비어 있으면 명확한 에러로 종료."""
    value = os.environ.get(name, "").strip()
    if not value:
        logger.error(
            "환경변수 %s 가 비어 있습니다. GitHub Secrets 또는 로컬 .env 확인 필요.",
            name,
        )
        sys.exit(1)
    return value


def _optional_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        if v <= 0:
            raise ValueError("must be > 0")
        return v
    except ValueError:
        logger.warning("환경변수 %s=%r 값이 양의 정수가 아님. 기본값 %d 사용.", name, raw, default)
        return default


def _today_kst() -> date:
    """현재 KST 날짜."""
    return datetime.now(tz=KST).date()


def _format_brod_dt(d: date) -> str:
    """API 쿼리용 'YYYYMMDD'."""
    return d.strftime("%Y%m%d")


# ----------------------------------------------------------------------------
# HMall fetch & parse
# ----------------------------------------------------------------------------


def _parse_param_dtm(param: Optional[str]) -> Optional[datetime]:
    """`brodStrtDtmParam`/`brodEndDtmParam` (14자리) → KST datetime."""
    if not isinstance(param, str):
        return None
    s = param.strip()
    if len(s) != 14 or not s.isdigit():
        return None
    try:
        return datetime(
            int(s[0:4]), int(s[4:6]), int(s[6:8]),
            int(s[8:10]), int(s[10:12]), int(s[12:14]),
            tzinfo=KST,
        )
    except ValueError:
        return None


def _parse_hhmm_with_date(hhmm: str, brod_dt: str) -> Optional[datetime]:
    """폴백: 'HH:MM' + 'YYYYMMDD' → KST datetime."""
    if not isinstance(hhmm, str) or ":" not in hhmm:
        return None
    if not isinstance(brod_dt, str) or len(brod_dt) != 8 or not brod_dt.isdigit():
        return None
    try:
        h, m = hhmm.split(":", 1)
        return datetime(
            int(brod_dt[0:4]), int(brod_dt[4:6]), int(brod_dt[6:8]),
            int(h), int(m), 0, tzinfo=KST,
        )
    except ValueError:
        return None


def _yn_to_bool(v: Any) -> bool:
    """'Y'/'N' → bool. 기타 값은 False."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().upper() == "Y"
    return False


def _safe_int(v: Any) -> Optional[int]:
    """int 변환 실패 시 None."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_day(client: httpx.Client, brod_dt: date) -> FetchResult:
    """HMall API 1일치 호출 + broadItemList 추출.

    실패 시 `FetchResult.ok=False` 와 error 메시지만 채우고 반환.
    """
    params = {
        "brodDt": _format_brod_dt(brod_dt),
        "brodPrrgPage": "0",
        "deviceInfo": "pc",
    }
    try:
        resp = client.get(HMALL_API_URL, params=params, headers=HMALL_HEADERS)
    except httpx.HTTPError as e:
        return FetchResult(brod_dt=brod_dt, ok=False, error=f"HTTP 실패: {e}")

    if resp.status_code // 100 != 2:
        return FetchResult(
            brod_dt=brod_dt,
            ok=False,
            error=f"HTTP {resp.status_code}",
        )

    try:
        payload = resp.json()
    except ValueError as e:
        return FetchResult(brod_dt=brod_dt, ok=False, error=f"JSON 파싱 실패: {e}")

    if not isinstance(payload, dict):
        return FetchResult(brod_dt=brod_dt, ok=False, error="응답이 JSON 객체가 아님")

    resp_data = payload.get("respData")
    if not isinstance(resp_data, dict):
        return FetchResult(brod_dt=brod_dt, ok=False, error="respData 누락")

    broad_list = resp_data.get("broadItemList")
    if not isinstance(broad_list, list):
        return FetchResult(brod_dt=brod_dt, ok=False, error="broadItemList 누락")

    resp_code = payload.get("respCode") or resp_data.get("respCode")
    # 빈 리스트인 경우는 코드 무관 정상 (그 날 편성 없음)
    if resp_code != HMALL_OK_RESP_CODE and len(broad_list) > 0:
        return FetchResult(
            brod_dt=brod_dt,
            ok=False,
            error=f"비정상 respCode: {resp_code!r}",
        )

    items = parse_items(broad_list)
    return FetchResult(brod_dt=brod_dt, items=items, ok=True)


def parse_items(raw_list: list[dict]) -> list[Item]:
    """broadItemList(list[dict]) → list[Item]. 각 아이템 단위로 안전 변환."""
    items: list[Item] = []
    for idx, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            logger.warning("broadItemList[%d] dict 아님, skip", idx)
            continue

        slitm_cd = raw.get("slitmCd")
        if not isinstance(slitm_cd, str) or not slitm_cd:
            logger.warning("broadItemList[%d] slitmCd 누락, skip", idx)
            continue

        # 시각: 14자리 param 우선, 폴백은 HH:MM + brodDt
        started = _parse_param_dtm(raw.get("brodStrtDtmParam"))
        ended = _parse_param_dtm(raw.get("brodEndDtmParam"))
        brod_dt_str = raw.get("brodDt") if isinstance(raw.get("brodDt"), str) else ""
        if started is None:
            started = _parse_hhmm_with_date(raw.get("brodStrtDtm", ""), brod_dt_str)
        if ended is None:
            ended = _parse_hhmm_with_date(raw.get("brodEndDtm", ""), brod_dt_str)
        if started is None or ended is None:
            logger.warning("broadItemList[%d] 시각 파싱 실패, skip (slitmCd=%s)", idx, slitm_cd)
            continue
        if ended <= started:
            ended = ended + timedelta(days=1)

        # 채널 코드 정규화
        channel = raw.get("brodChnlNm")
        if not isinstance(channel, str):
            logger.warning("broadItemList[%d] brodChnlNm 누락, skip", idx)
            continue
        channel = channel.strip().lower()
        if channel not in CHANNEL_DISPLAY:
            logger.warning("broadItemList[%d] 미지 채널 %r, skip", idx, channel)
            continue

        # 상품명: convertedSlitmNm 우선
        converted = raw.get("convertedSlitmNm")
        slitm_nm = (
            converted.strip()
            if isinstance(converted, str) and converted.strip()
            else (raw.get("slitmNm") or "").strip()
        )
        if not slitm_nm:
            slitm_nm = slitm_cd  # 최후 폴백

        brnd_nm_raw = raw.get("brndNm")
        brnd_nm = brnd_nm_raw.strip() if isinstance(brnd_nm_raw, str) and brnd_nm_raw.strip() else None

        exposure_order = _safe_int(raw.get("bitmExpsOrdg")) or 0

        items.append(
            Item(
                slitm_cd=slitm_cd,
                slitm_nm=slitm_nm,
                brnd_nm=brnd_nm,
                channel_code=channel,
                started_at=started,
                ended_at=ended,
                bbprc=_safe_int(raw.get("bbprc")),
                sell_prc=_safe_int(raw.get("sellPrc")),
                dc_rate=_safe_int(raw.get("dcRate")),
                live_yn=_yn_to_bool(raw.get("liveYn")),
                exposure_order=exposure_order,
            )
        )
    return items


# ----------------------------------------------------------------------------
# 슬롯 그룹핑
# ----------------------------------------------------------------------------


def group_into_slots(items: Iterable[Item]) -> dict[tuple[str, str, str], list[Item]]:
    """(channel_code, started_at iso, ended_at iso) 기준 그룹핑.

    각 그룹은 exposure_order 오름차순으로 정렬. 반환 dict 는 시간순 정렬됨.
    """
    groups: dict[tuple[str, str, str], list[Item]] = {}
    for it in items:
        groups.setdefault(it.slot_key, []).append(it)
    for k in groups:
        groups[k].sort(key=lambda x: (x.exposure_order, x.slitm_cd))

    # 시간순 정렬된 dict 재구성 (channel 은 mtv/dtv/etv 순)
    channel_order = {"mtv": 0, "dtv": 1, "etv": 2}

    def _sort_key(k: tuple[str, str, str]) -> tuple[str, int]:
        return (k[1], channel_order.get(k[0], 99))

    return {k: groups[k] for k in sorted(groups.keys(), key=_sort_key)}


# ----------------------------------------------------------------------------
# Notion 블록 빌더
# ----------------------------------------------------------------------------


def _rt(content: str, *, bold: bool = False, code: bool = False,
        italic: bool = False, strikethrough: bool = False,
        link: Optional[str] = None) -> dict:
    """rich_text 한 런(run) 생성 헬퍼."""
    text_obj: dict[str, Any] = {"content": content}
    if link:
        text_obj["link"] = {"url": link}
    return {
        "type": "text",
        "text": text_obj,
        "annotations": {
            "bold": bold,
            "italic": italic,
            "strikethrough": strikethrough,
            "underline": False,
            "code": code,
            "color": "default",
        },
    }


def _format_time_range(started: datetime, ended: datetime) -> str:
    """'HH:MM ~ HH:MM' (KST)."""
    return f"{started.strftime('%H:%M')} ~ {ended.strftime('%H:%M')}"


def _build_price_runs(item: Item) -> list[dict]:
    """가격 표기를 rich_text 런 리스트로. 빈 리스트면 가격 미공개.

    규칙:
      - bbprc < sell_prc: "62,910원 ~~69,900원~~ (10% ↓)"  (sell_prc 만 strikethrough)
      - 동일 또는 sell_prc 없음: "59,900원"
      - bbprc null/0: "가격 미공개"
    """
    bbprc = item.bbprc
    sell_prc = item.sell_prc
    dc_rate = item.dc_rate

    if not bbprc or bbprc <= 0:
        return [_rt("가격 미공개", italic=True)]

    if sell_prc and sell_prc > bbprc:
        runs = [_rt(f"{bbprc:,}원", bold=True), _rt(" ")]
        runs.append(_rt(f"{sell_prc:,}원", strikethrough=True))
        if dc_rate and dc_rate > 0:
            runs.append(_rt(f" ({dc_rate}% ↓)"))
        return runs

    return [_rt(f"{bbprc:,}원", bold=True)]


def _build_item_bullet(item: Item) -> dict:
    """슬롯 내 상품 1개 → bulleted_list_item 블록.

    포맷: **[상품명](링크)**  ·  코드 `slitmCd`  ·  브랜드 X  ·  가격
    """
    runs: list[dict] = [
        _rt(item.slitm_nm, bold=True, link=item.detail_url),
        _rt("  ·  코드 "),
        _rt(item.slitm_cd, code=True),
    ]
    if item.brnd_nm:
        runs.append(_rt(f"  ·  브랜드 {item.brnd_nm}"))
    runs.append(_rt("  ·  "))
    runs.extend(_build_price_runs(item))

    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": runs},
    }


def _build_slot_heading(slot_items: list[Item]) -> dict:
    """슬롯 헤더 (heading_3): '09:00 ~ 10:00 · TV쇼핑'  + LIVE 표기."""
    first = slot_items[0]
    chan_label = CHANNEL_DISPLAY.get(first.channel_code, first.channel_code)
    label = f"{_format_time_range(first.started_at, first.ended_at)}  ·  {chan_label}"
    runs: list[dict] = [_rt(label, bold=True)]
    # 슬롯 안 어느 아이템이라도 live 면 LIVE 표기
    if any(it.live_yn for it in slot_items):
        runs.append(_rt("  ", bold=True))
        runs.append(_rt("LIVE", bold=True, code=True))
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": runs},
    }


def _build_day_heading(d: date) -> dict:
    """일자 헤더 (heading_2): '2026-05-13 (화)'."""
    label = f"{d.isoformat()} ({WEEKDAY_KR[d.weekday()]})"
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_rt(label, bold=True)]},
    }


def _build_no_schedule_block() -> dict:
    """편성 없는 날: '편성 없음' italic."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_rt("편성 없음", italic=True)]},
    }


def _build_header_blocks(
    now_kst: datetime,
    days: list[date],
    failed_days: list[date],
) -> list[dict]:
    """페이지 상단: 제목(H1) + 메타(quote)."""
    title_block = {
        "object": "block",
        "type": "heading_1",
        "heading_1": {"rich_text": [_rt("현대홈쇼핑 편성표", bold=True)]},
    }

    success_n = len(days) - len(failed_days)
    total_n = len(days)
    if failed_days:
        failed_str = ", ".join(d.isoformat() for d in failed_days)
        status_text = (
            f"{success_n}/{total_n} 일 성공 ({failed_str} 실패)"
        )
    else:
        status_text = f"{total_n}/{total_n} 일 성공"

    quote_runs = [
        _rt("최종 업데이트: "),
        _rt(now_kst.strftime("%Y-%m-%d %H:%M KST"), bold=True),
        _rt("  ·  "),
        _rt(status_text),
    ]
    quote_block = {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": quote_runs},
    }
    return [title_block, quote_block]


def build_blocks(
    by_date: dict[date, list[Item]],
    failed_days: list[date],
    now_kst: datetime,
) -> list[dict]:
    """전체 페이지의 children 블록 리스트 생성.

    구조:
      heading_1 / quote
      ─ 일자 반복 ─
        heading_2 (날짜)
        (편성 있음) heading_3 / bullet... 반복
        (편성 없음) paragraph "편성 없음"
      divider (마지막)
    """
    days = sorted(by_date.keys())
    blocks: list[dict] = []
    blocks.extend(_build_header_blocks(now_kst, days, failed_days))

    for d in days:
        blocks.append(_build_day_heading(d))
        day_items = by_date[d]
        if not day_items:
            blocks.append(_build_no_schedule_block())
            continue
        slots = group_into_slots(day_items)
        for slot_items in slots.values():
            blocks.append(_build_slot_heading(slot_items))
            for it in slot_items:
                blocks.append(_build_item_bullet(it))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    return blocks


def build_failure_blocks(now_kst: datetime, days: list[date]) -> list[dict]:
    """전체 fetch 실패 시 페이지에 남길 안내 블록."""
    return [
        {
            "object": "block",
            "type": "heading_1",
            "heading_1": {"rich_text": [_rt("현대홈쇼핑 편성표", bold=True)]},
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [
                    _rt("데이터 수집 실패", bold=True),
                    _rt(
                        f" — {now_kst.strftime('%Y-%m-%d %H:%M KST')} 실행에서 "
                        f"{len(days)}일치 모두 fetch 실패. 다음 자동 실행에서 자동 회복됩니다."
                    ),
                ],
                "icon": {"type": "emoji", "emoji": "warning"},
                "color": "red_background",
            },
        },
    ]


# ----------------------------------------------------------------------------
# Notion 페이지 자식 정리 + 신규 추가
# ----------------------------------------------------------------------------


def _retry(func, *, attempts: int = 3, base_delay: float = 1.5, what: str = "Notion API"):
    """지수 백오프 재시도. 마지막 시도까지 실패하면 마지막 예외를 raise."""
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return func()
        except (APIResponseError, HTTPResponseError, RequestTimeoutError, httpx.HTTPError) as e:
            last_exc = e
            wait = base_delay * (2 ** i)
            logger.warning(
                "%s 실패 (시도 %d/%d): %s — %.1f초 후 재시도",
                what, i + 1, attempts, e, wait,
            )
            if i < attempts - 1:
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def _list_all_child_block_ids(notion: NotionClient, page_id: str) -> list[str]:
    """페이지의 모든 자식 블록 ID 를 수집 (페이지네이션)."""
    ids: list[str] = []
    cursor: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = _retry(
            lambda: notion.blocks.children.list(**kwargs),
            what="blocks.children.list",
        )
        for b in resp.get("results", []):
            bid = b.get("id")
            if bid:
                ids.append(bid)
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return ids


def replace_page_content(
    notion: NotionClient, page_id: str, blocks: list[dict]
) -> None:
    """페이지의 기존 자식 블록을 모두 삭제하고 새 블록을 청크로 append.

    Notion API:
      - blocks.children.append: 한 번에 최대 100개
      - blocks.delete: 블록 단위 삭제 (= archive)
    """
    # 1) 기존 자식 모두 삭제
    existing_ids = _list_all_child_block_ids(notion, page_id)
    logger.info("기존 자식 블록 %d개 삭제 시작", len(existing_ids))
    for bid in existing_ids:
        _retry(
            lambda b=bid: notion.blocks.delete(block_id=b),
            what=f"blocks.delete({bid[:8]})",
        )
    logger.info("기존 자식 블록 삭제 완료")

    # 2) 새 블록 청크 단위 append
    total = len(blocks)
    logger.info("새 블록 %d개 append 시작 (청크 %d개씩)", total, NOTION_APPEND_CHUNK)
    for i in range(0, total, NOTION_APPEND_CHUNK):
        chunk = blocks[i:i + NOTION_APPEND_CHUNK]
        _retry(
            lambda c=chunk: notion.blocks.children.append(
                block_id=page_id, children=c
            ),
            what=f"blocks.children.append({i}-{i + len(chunk)})",
        )
    logger.info("새 블록 append 완료")


# ----------------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------------


def main() -> int:
    """진입점. 종료 코드 반환 (0=성공, 1=치명적 실패)."""
    # 1) 환경변수
    notion_token = _require_env("NOTION_TOKEN")
    page_id = _require_env("NOTION_PAGE_ID")
    days_ahead = _optional_int_env("DAYS_AHEAD", 7)

    now_kst = datetime.now(tz=KST)
    today = now_kst.date()
    days: list[date] = [today + timedelta(days=i) for i in range(days_ahead)]

    logger.info(
        "실행 시작 — 기준=%s, %d일치 fetch (page=%s***)",
        today.isoformat(), days_ahead, page_id[:8],
    )

    # 2) HMall fetch
    by_date: dict[date, list[Item]] = {d: [] for d in days}
    failed_days: list[date] = []
    total_items = 0

    with httpx.Client(timeout=15.0) as client:
        for d in days:
            result = fetch_day(client, d)
            if result.ok:
                by_date[d] = result.items
                total_items += len(result.items)
                logger.info("fetch ok: %s — %d items", d.isoformat(), len(result.items))
            else:
                failed_days.append(d)
                logger.warning("fetch 실패: %s — %s", d.isoformat(), result.error)

    # 3) Notion 클라이언트
    notion = NotionClient(auth=notion_token)

    # 4) 전체 실패 vs 일부 성공 분기
    if len(failed_days) == len(days):
        logger.error("모든 날짜 fetch 실패. 페이지에 실패 안내문만 남깁니다.")
        try:
            replace_page_content(
                notion, page_id, build_failure_blocks(now_kst, days)
            )
        except Exception as e:
            logger.error("실패 안내 페이지 갱신도 실패: %s", e)
        return 1

    # 5) 블록 빌드 + 페이지 교체
    blocks = build_blocks(by_date, failed_days, now_kst)
    total_slots = sum(len(group_into_slots(items)) for items in by_date.values())

    try:
        replace_page_content(notion, page_id, blocks)
    except Exception as e:
        logger.error("Notion 페이지 갱신 실패 (재시도 3회 모두 소진): %s", e)
        return 1

    # 6) 결과 보고
    success_n = len(days) - len(failed_days)
    failed_str = (
        ", ".join(d.isoformat() for d in failed_days) if failed_days else "없음"
    )
    print(
        f"OK: 페이지 갱신 완료, {success_n}/{len(days)}일치, "
        f"{total_slots}슬롯, {total_items}상품, 실패일: {failed_str}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
