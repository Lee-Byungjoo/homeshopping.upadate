# HMall 편성표 → Notion 자동 발행

## 무엇을 하는 프로젝트인가

매일 아침 한국시간(KST) 10시에 **현대홈쇼핑(HMall) 편성표 7일치**를 자동으로 가져와서 지정된 **Notion 페이지**에 정리해 넣는 미니 프로젝트입니다. GitHub Actions 가 무료로 스케줄러 역할을 해주기 때문에 본인 PC 가 꺼져 있어도 매일 알아서 돕니다. 한 번 셋업하면 잊고 살 수 있습니다.

---

## 사전 준비물

- **GitHub 계정** — 없으면 [github.com/signup](https://github.com/signup) 에서 5분이면 만듭니다. 무료.
- **Notion 계정** — 이미 있으실 거예요.
- **대상 Notion 페이지** — 편성표가 매일 갱신될 페이지 한 개. 이 프로젝트의 기본 대상은:
  `https://uneven-catboat-891.notion.site/35e5b3b4a3e880c0bcd3d810f896e8f7`
  다른 페이지로 바꾸려면 8단계 환경변수만 다르게 넣으면 됩니다.

---

## 셋업 (순서가 중요합니다)

### 1. Notion Integration 생성 및 토큰 발급

1. https://www.notion.so/my-integrations 접속
2. 우측 상단 **"New integration"** 클릭
3. 폼 작성:
   - **Name**: `HMall Schedule Bot` (아무거나 OK)
   - **Associated workspace**: 본인 워크스페이스 선택
   - **Type**: `Internal` (기본값 그대로)
4. **Save** 클릭
5. 다음 화면에서 **"Internal Integration Secret"** 우측 **Show** → **Copy** 클릭. `secret_` 로 시작하는 문자열입니다. 이게 `NOTION_TOKEN` 값입니다. 메모장에 잠깐 붙여두세요.

> 이 토큰은 한 번 잃어버리면 다시 못 봅니다(재발급은 가능). 노출되지 않게 주의하세요.

---

### 2. Notion 페이지에 Integration 권한 부여

이 단계를 빠뜨리면 스크립트가 401 에러로 무조건 실패합니다. **가장 흔한 실수입니다.**

1. 대상 Notion 페이지를 브라우저에서 엽니다
2. 우측 상단 **점 세 개(...) 메뉴** 클릭
3. 메뉴에서 **"Connections"** (또는 **"Add connections"**) 찾기
   - Notion UI 버전에 따라 **"Add connections"** 가 바로 보일 수도, **"Connections"** 서브메뉴 안에 있을 수도 있습니다
4. 검색창에 `HMall` 입력 → 1단계에서 만든 **"HMall Schedule Bot"** 클릭
5. **"Confirm"** 으로 권한 부여 확인

> 이 단계가 끝나면 Integration 이 그 페이지(및 하위 페이지)에 글을 쓸 수 있습니다. 다른 페이지/DB 는 못 봅니다.

---

### 3. 페이지 ID 확보

페이지 URL 의 끝부분 32자리 hex 가 페이지 ID 입니다.

예) `https://uneven-catboat-891.notion.site/35e5b3b4a3e880c0bcd3d810f896e8f7`
→ ID 는 `35e5b3b4a3e880c0bcd3d810f896e8f7`

대시(`-`)가 있는 형태(`35e5b3b4-a3e8-80c0-bcd3-d810f896e8f7`)도 그대로 동작합니다.

---

### 4. GitHub repo 생성

1. github.com 로그인
2. 우측 상단 **+ → New repository**
3. 폼 작성:
   - **Repository name**: `hmall-schedule-publisher` (아무거나)
   - **Public** 또는 **Private** 둘 다 OK
     - Public 이어도 Secrets 은 별도 보호되므로 토큰 노출 걱정 없음
     - GitHub Actions 무료 한도: Public repo 는 **무제한**, Private 는 월 2,000분
   - **Add a README file** 체크박스는 **풀어두기** (이 폴더의 README 를 그대로 올릴 거니까)
   - `.gitignore`, `License` 도 풀어두기
4. **Create repository** 클릭

---

### 5. 로컬에서 코드 push

이 `github-setup/` 폴더 안의 6개 파일을 새 repo 에 그대로 올립니다.

**방법 A — Git 커맨드라인** (이미 git 깔려 있고 익숙한 분)

```bash
cd /이-폴더-경로/github-setup
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/hmall-schedule-publisher.git
git push -u origin main
```

처음 push 시 GitHub 계정 인증을 요구할 수 있습니다. 비밀번호 대신 [Personal Access Token](https://github.com/settings/tokens) 사용을 권장합니다.

**방법 B — GitHub Desktop** (GUI 선호)

1. https://desktop.github.com/ 설치
2. **File → New repository** 또는 **Add → Add existing repository** 로 이 폴더 선택
3. **Publish repository** 클릭 → 위에서 만든 repo 이름 입력

**방법 C — 웹에서 드래그&드롭**

1. 4단계에서 만든 빈 repo 페이지 열기
2. **"uploading an existing file"** 링크 클릭
3. 이 폴더의 모든 파일(숨김 파일 `.github/`, `.gitignore`, `.env.example` 포함) 드래그
4. 커밋 메시지 입력 후 **Commit changes**
   - 이 방법은 `.github/workflows/daily.yml` 같은 하위 폴더 구조 보존이 까다로워서 비추. 가능하면 A 나 B 권장.

---

### 6. GitHub Secrets 등록

스크립트에 토큰을 안전하게 전달합니다.

1. repo 페이지 → 상단 탭 **Settings**
2. 좌측 사이드바 **Secrets and variables → Actions**
3. **"New repository secret"** 버튼으로 두 개 등록:

| Name | Secret 값 |
|---|---|
| `NOTION_TOKEN` | 1단계에서 복사한 `secret_...` 문자열 |
| `NOTION_PAGE_ID` | `35e5b3b4a3e880c0bcd3d810f896e8f7` (또는 본인 페이지 ID) |

> Secrets 는 한 번 등록하면 다시 못 봅니다 (수정/삭제만 가능). 노출 걱정 X.

---

### 7. Actions 활성화 및 수동 테스트

1. repo 페이지 → 상단 탭 **Actions**
2. 노란 안내 배너 뜨면 **"I understand my workflows, go ahead and enable them"** 클릭
3. 좌측 워크플로우 목록에서 **"HMall daily schedule publish"** 클릭
4. 우측 **"Run workflow"** 드롭다운 → 브랜치 `main` 선택 → 초록 **"Run workflow"** 버튼
5. 1~2분 기다리면 새 실행 항목이 생깁니다. 클릭해서 로그 확인.
6. 초록 체크 ✓ 면 성공! Notion 페이지를 열어보면 편성표 7일치가 들어가 있을 겁니다.
7. 빨강 X 면 로그 클릭 → 거의 100% 2단계(Integration 권한 부여) 누락입니다. 다시 확인.

---

### 8. 자동 실행 확인

- 위 워크플로우는 **매일 UTC 01:00 (KST 오전 10시)** 에 자동으로 실행됩니다
- repo → Actions 탭에서 매일 새 실행 기록이 쌓이는지 확인하면 됩니다
- 1회 실행에 보통 30초~1분 정도. Private repo 라도 월 60분 안쪽이라 무료 한도 충분.

> 셋업 끝! 이후로는 매일 아침 Notion 페이지가 알아서 갱신됩니다.

---

## 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| 로그에 `Unauthorized` (401) | Integration 이 페이지에 연결되지 않음 — **2단계** 다시 수행 |
| 로그에 `not_found` (404) | `NOTION_PAGE_ID` 오타이거나 페이지가 다른 워크스페이스에 있음 — **3단계, 6단계** 다시 확인 |
| 로그에 `Connect timeout` 또는 `HTTP 5xx` | HMall API 일시 불통. 일부 날짜만 실패하면 페이지에 "5/7 일 성공" 으로 표시되고, 다음 자동 실행에서 자동 회복됨 |
| 워크플로우가 시간 맞춰 안 돔 | GitHub Actions schedule 은 트래픽 많은 시간엔 분 단위로 지연될 수 있습니다 (정상) |
| Secrets 등록했는데도 `환경변수 NOTION_TOKEN 가 비어 있습니다` 에러 | Secret 이름 오타. 대소문자 정확히 `NOTION_TOKEN` / `NOTION_PAGE_ID` 여야 함 |

---

## (참고) 로컬에서 직접 테스트하기

GitHub 에 올리기 전에 본인 PC 에서 한 번 돌려볼 수도 있습니다.

```bash
cd github-setup
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# .env.example 을 복사해서 .env 만들고 값 채우기
cp .env.example .env
# .env 파일을 텍스트 에디터로 열어 NOTION_TOKEN / NOTION_PAGE_ID 입력

# .env 의 값을 환경변수로 export 후 실행 (PowerShell 예시)
$env:NOTION_TOKEN="secret_..."
$env:NOTION_PAGE_ID="35e5b3b4a3e880c0bcd3d810f896e8f7"
python publish.py
```

Python 3.11 이상 필요 (`zoneinfo` 표준 라이브러리 사용).

---

## 데이터 안전성

- **HMall API 응답에 개인 정보 없음** — 상품/방송 정보만 포함, 비로그인 호출로 확인됨
- **Notion Integration 권한 범위가 좁다** — 2단계에서 명시적으로 연결한 페이지만 접근 가능. 워크스페이스 다른 페이지/DB 는 못 봄
- **NOTION_TOKEN 은 GitHub Secrets 에 암호화 저장** — 워크플로우 로그에 자동 마스킹되어 노출되지 않음
- **이 프로젝트는 외부 호출이 두 군데 뿐**: HMall 공개 API (GET) + Notion 공식 API (자기 토큰)

---

## 파일 구성

```
github-setup/
├── publish.py              # 메인 스크립트 (한 파일에 모든 로직)
├── requirements.txt        # 파이썬 의존성 2개
├── README.md               # 이 파일
├── .gitignore              # __pycache__, .env 등 제외
├── .env.example            # 로컬 테스트용 env 템플릿
└── .github/
    └── workflows/
        └── daily.yml       # GitHub Actions 스케줄 정의
```
