"""
발렛 자동 예약 봇 (내 PC / GitHub Actions 겸용)
  1) 출국일을 눌러 빈자리인지 확인  2) 도착일 선택  3) 정보 입력  4) (옵션) 등록하기

설정은 같은 폴더의 local.env 파일에서 읽습니다. (개인정보는 코드에 넣지 않음)
"""
import os, re, sys, time, random, datetime, urllib.request
from playwright.sync_api import sync_playwright

URL = "https://valet.amanopark.co.kr/booking"
KST = datetime.timezone(datetime.timedelta(hours=9))  # 한국 시간 (윈도우 시간대 DB 불필요)


# ---------- 설정 읽기 ----------
def load_env(path="local.env"):
    if not os.path.exists(path):
        return
    raw = open(path, "rb").read()
    text = ""
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not os.environ.get(k):          # GitHub Variables 등으로 이미 지정된 값이 있으면 그걸 우선
            os.environ[k] = v.strip().strip('"').strip("'")


load_env()
G = os.environ.get
IN_CI = G("CI", "").lower() == "true"
DEPART_DATE = G("DEPART_DATE", "")
RETURN_DATE = G("RETURN_DATE", "")
NAME, PHONE, CAR = G("NAME", ""), G("PHONE", ""), G("CAR_NUMBER", "")
AIRLINE = G("AIRLINE", "대한항공")
AUTO_SUBMIT = G("AUTO_SUBMIT", "false").lower() == "true"
NTFY_TOPIC = G("NTFY_TOPIC", "")
HEADLESS = IN_CI or G("HEADLESS", "false").lower() == "true"
LOOP = G("LOOP", "false" if IN_CI else "true").lower() == "true"
INTERVAL = int(G("INTERVAL", "60"))
MAX_MINUTES = int(G("MAX_MINUTES", "0"))   # 0이면 제한 없음. GitHub에서는 한 번 실행에 이 시간만큼 반복


def scrub(text):
    """로그에 개인정보가 남지 않게 가림 (GitHub 로그는 공개될 수 있음)"""
    text = str(text).splitlines()[0][:200] if str(text) else ""
    for secret in (NAME, PHONE, CAR, NTFY_TOPIC):
        if secret:
            text = text.replace(secret, "***")
    return text


def scrub_text(text):
    for secret in (NAME, PHONE, CAR, NTFY_TOPIC):
        if secret:
            text = text.replace(secret, "***")
    return text


def log(msg):
    print(f"[{datetime.datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def push(msg):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
            headers={"Priority": "urgent", "Title": "Valet"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"알림 전송 실패: {scrub(e)}")


# ---------- 달력 ----------
def pick_date(page, idx, date_str):
    """idx 0 = 출발일 칸, 1 = 도착일 칸"""
    y, m, d = map(int, date_str.split("-"))
    page.get_by_role("textbox", name="년도-월-일").nth(idx).click()
    page.wait_for_timeout(500)
    panels = page.locator(".el-picker-panel:visible")
    panel = panels.last if panels.count() else None

    # 현재 달력이 몇 월을 보여주는지 읽어서 이동 (실패하면 오늘 기준으로 계산)
    ahead = None
    if panel is not None:
        try:
            head = panel.locator(".el-date-picker__header").first.inner_text()
            nums = [int(x) for x in re.findall(r"\d+", head)]
            if len(nums) >= 2 and 2000 < nums[0] < 2100 and 1 <= nums[1] <= 12:
                ahead = (y - nums[0]) * 12 + (m - nums[1])
        except Exception:
            pass
    if ahead is None:
        t = datetime.datetime.now(KST).date()
        ahead = (y - t.year) * 12 + (m - t.month)
    btn = "Next Month" if ahead > 0 else "Previous Month"
    for _ in range(abs(ahead)):
        scope = panel if panel is not None else page
        scope.get_by_role("button", name=btn).first.click()
        page.wait_for_timeout(300)

    # 날짜 칸 클릭
    if panel is not None:
        pat = re.compile(rf"^\s*{d}\s*$")
        cell = panel.locator("td.available:not(.prev-month):not(.next-month)").filter(has_text=pat)
        if cell.count() == 0:
            cell = panel.locator("td:not(.prev-month):not(.next-month)").filter(has_text=pat)
        if cell.count():
            cell.first.click()
            return
    cells = page.get_by_text(str(d), exact=True)     # 예비 방법
    (cells.first if d < 15 else cells.last).click()


def closed_toast(page):
    """'예약이 불가합니다' 안내가 뜨면 True (해외 서버는 응답이 느려서 넉넉히 기다림)"""
    page.wait_for_timeout(2000)
    try:
        page.get_by_text("예약이 불가").first.wait_for(state="visible", timeout=6000)
        return True
    except Exception:
        return False


def toast_visible_now(page):
    try:
        return page.get_by_text("예약이 불가").first.is_visible()
    except Exception:
        return False


def has_date(page, date_str):
    target = date_str.replace("-", "")
    for box in page.locator("input").all():
        try:
            digits = "".join(c for c in box.input_value() if c.isdigit())
            if target in digits:
                return True
        except Exception:
            pass
    return False


# ---------- 입력 ----------
def fill_form(page):
    for ph, val in (("한글 또는 영문으로 입력해 주세요", NAME),
                    ("'-'없이 숫자만 입력해 주세요", PHONE),
                    ("차량번호를 입력해 주세요. (00가0000)", CAR)):
        box = page.get_by_role("textbox", name=ph)
        box.click()
        box.fill(val)

    # 출발 항공편: 라벨 기준으로 찾고, 실패하면 녹화 때 쓴 번호(5)로
    box = None
    lab = page.get_by_text("출발 항공편", exact=True)
    if lab.count():
        cand = lab.first.locator("xpath=following::input[contains(@placeholder,'선택해주세요')][1]")
        if cand.count():
            box = cand.first
    if box is None:
        box = page.get_by_role("textbox", name="선택해주세요").nth(5)
    box.click()
    page.get_by_role("listitem").filter(has_text=AIRLINE).first.click()

    # 약관 동의
    try:
        page.locator(".component-footer-checkbox > .el-checkbox > .el-checkbox__input > .el-checkbox__inner").click()
    except Exception:
        pass
    cb = page.get_by_role("checkbox", name="위 약관에 모두 동의합니다")
    if not cb.is_checked():
        cb.check(force=True)


SUBMIT_CLICKED = False   # '등록하기'를 누른 뒤에는 오류가 나도 재시도하지 않음 (중복 예약 방지)


MSG_SELECTOR = ".el-message, .el-message-box, .el-notification, .el-dialog, [role=dialog], [role=alert]"


def visible_msgs(page):
    out = []
    try:
        els = page.locator(MSG_SELECTOR)
        for i in range(min(els.count(), 5)):
            if els.nth(i).is_visible():
                out.append(els.nth(i).inner_text()[:200].replace("\n", " / "))
    except Exception:
        pass
    return out


def submit(page):
    """반환: 'booked'(접수됨) / 'rejected'(사이트가 거절, 재시도 가능)"""
    global SUBMIT_CLICKED
    page.get_by_role("button", name="등록하기").click()
    SUBMIT_CLICKED = True
    # 금방 사라지는 안내(토스트)를 놓치지 않도록 클릭 직후 약 2초간 계속 읽음
    seen = []
    for _ in range(8):
        page.wait_for_timeout(250)
        for t in visible_msgs(page):
            if t not in seen:
                seen.append(t)
        if page.get_by_role("button", name="확인").count():
            break
    shown = scrub_text(" | ".join(seen)) if seen else "(보이는 안내 없음)"
    log(f"등록하기 직후 안내: {shown}")
    if any(w in shown for w in ("불가", "초과", "마감", "실패", "이미")):
        SUBMIT_CLICKED = False      # 거절됐으므로 다시 시도해도 중복 예약이 아님
        return "rejected"
    try:
        page.get_by_role("button", name="확인").click(timeout=10000)
    except Exception:
        log(f"확인 버튼 없음. 현재 주소: {page.url.split('?')[0]}")
        raise
    page.wait_for_timeout(3000)
    return "booked"


# ---------- 1회 시도 ----------
def attempt(page):
    page.goto(URL, wait_until="networkidle")
    pick_date(page, 0, DEPART_DATE)
    if closed_toast(page):
        return "closed"
    if not has_date(page, DEPART_DATE):
        return "unknown"
    pick_date(page, 1, RETURN_DATE)
    if closed_toast(page):
        return "closed_return"
    if not has_date(page, RETURN_DATE):
        return "unknown"
    log("출국일/도착일 확보! 정보 입력 시작")
    fill_form(page)
    if toast_visible_now(page):      # 입력하는 사이에 뒤늦게 마감 안내가 뜬 경우
        return "closed"
    if not AUTO_SUBMIT:
        return "filled"
    return submit(page)


def set_output(key, val):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={val}\n")


def main():
    missing = [k for k, v in (("DEPART_DATE", DEPART_DATE), ("RETURN_DATE", RETURN_DATE),
                              ("NAME", NAME), ("PHONE", PHONE), ("CAR_NUMBER", CAR)) if not v]
    if missing:
        log(f"local.env에 빠진 항목: {', '.join(missing)}")
        sys.exit(0 if IN_CI else 1)
    log(f"시작 (출국 {DEPART_DATE} / 도착 {RETURN_DATE} / 자동제출 {'ON' if AUTO_SUBMIT else 'OFF'})")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = bro
