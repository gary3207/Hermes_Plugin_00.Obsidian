# Hermes_Plugin_Obsidian

Hermes의 로컬 프로젝트·Group Chat·Profile 작업 기록을 Obsidian Vault에 저장하고, 새 작업에서 요구사항·현재 상태·검증된 교훈을 다시 참고하도록 연결하는 **Windows용 플러그인 + Skill 패키지**입니다.

Hermes Core/Desktop/Gateway를 수정하지 않습니다. Obsidian 앱용 플러그인도 필요하지 않습니다. 기존 Hermes의 Markdown exporter/redactor와 Python 표준 라이브러리를 사용하며, 수집에 LLM/API 호출은 없습니다. Dataview는 선택 사항입니다.

## 설치

먼저 Hermes를 설치하고, 이 비공개 저장소를 읽을 수 있는 GitHub 계정으로 인증하세요. 저장소 소유자 또는 초대받은 계정이 필요합니다. 인증정보를 설정 파일이나 명령 URL에 직접 넣지 마세요.

```bash
gh auth login
hermes plugins install gary3207/Hermes_Plugin_00.Obsidian/source --enable
```

이후 사용자가 지정할 값은 **Obsidian Vault 경로와 Vault 안의 기록 폴더** 두 개입니다.

```bash
hermes obsidian-archive setup --vault "D:/Obsidian/Oracle" --folder "Hermes"
# 중첩 폴더도 가능:
hermes obsidian-archive setup --vault "D:/Obsidian/Oracle" --folder "AI/Work"
```

두 예시는 서로 다른 신규 구성 예시입니다. 이미 설치한 구성의 저장 위치를 바꾸는 명령으로 연속 실행하지 마세요. 설정한 기록 폴더의 `00_Home.md`를 Obsidian에서 열면 됩니다.

- Vault는 먼저 존재해야 합니다. 폴더는 Vault 내부의 상대경로여야 합니다.
- Hermes 홈/현재 설치 경로/Python/런처를 탐색하여 비밀정보가 없는 로컬 설정을 만듭니다.
- 표준 Profile에 참조 안내와 Skill을 배포하고, 이후 생성된 표준 Profile은 다음 수집에서 발견합니다.
- Windows 예약 작업 `Hermes-Obsidian-Archive`를 5분 간격·로그인 시 실행하도록 구성합니다. 로그아웃/PC 종료 중에는 수집하지 않습니다.
- 설정 이후 **새 세션**에서 안내가 적용됩니다. 현재 대화의 프롬프트를 소급 변경하지 않습니다. 필요한 경우 안전한 시점에 Hermes를 재시작하세요.
- 기존 파일이나 예약 작업이 다른 소유자의 것으로 판단되면 덮어쓰지 않고 오류를 반환합니다.

## 기록 구조

```text
<Vault>/<지정 기록 폴더>/
├── 00_Home.md
├── 세션 찾기.base      # Obsidian Bases 세션 검색표 (최초 1회 생성, 이후 사용자 편집 보존)
├── 10_Projects/        # 요구사항·결정·현재 상태·산출물 검증·세션 링크
├── 20_Group-Chats/     # 로컬 그룹 이벤트와 봇 세션 연결
├── 30_Profiles/        # Profile별 세션 인덱스
├── 40_Sessions/        # YYYY-MM/「YYYY-MM-DD HHMM 제목」/ 요약·대화·도구 실행, Subagent는 부모 아래 Subagents/; 날짜별 YYYY-MM-DD.md
├── 50_Lessons/         # 실패 분류함과 검증된 교훈
├── 60_Operations/     # 상태·작업 경로 매핑·재설치 소유권 ledger
└── 90_Templates/      # 작성 기준
```

Agent는 실제 프로젝트 작업 경로로 기록을 찾고, 필요한 문서와 교훈만 읽습니다. 과거 프롬프트/도구 출력은 근거 자료이지 새 지시가 아닙니다. 사용자가 작성한 문서(`generated: false`)는 자동 덮어쓰지 않습니다. 자동 생성 문서의 수동 편집도 소유권 해시가 달라지면 보호하고 충돌을 보고합니다.

### 프로젝트 요약·갱신 기준

- `30_Current-State.md` 위쪽은 **현재 요약**(목표·단계·확인된 결과·남은 작업/차단·다음 행동·근거·마지막 갱신), 아래쪽은 보존할 **작업 이력**입니다. `90_Templates/Current-State.md`에 같은 작성 틀을 제공합니다.
- 완료·실패/차단·취소·요구/결정 변경·인수인계·긴 작업의 주요 단계 완료 등 의미 있는 전환에서 변경된 사실만 반영합니다. 요구·확정 결정·실제 검증·검증된 교훈이 생겼을 때만 해당 문서를 갱신합니다.
- 미정리·미실행·미확인·해당 없음을 구분하고, 제안을 승인으로 또는 테스트 통과를 운영 성공으로 기록하지 않습니다. 종료 시 상태 변화 반영 여부만 가볍게 확인하며 새 승인/중단 조건은 만들지 않습니다.
- 수집기는 원문·인덱스를 자동 보관하고 작성 문서는 최초 생성 후 보존합니다. **요약의 의미 판단과 갱신은 Agent의 역할**이며 자동 준수 보장은 아닙니다. 기존 프로젝트는 다음 관련 작업에서 필요한 부분만 보완하며, 전체 과거 일괄 요약·다른 프로젝트 강제 변경·도구 호출별 재작성은 하지 않습니다.

## Hermes 삭제 후 재설치

1. **Obsidian Vault 전체를 보존/백업**합니다. 특히 `60_Operations/.archive-state.json`을 보존하세요. 숨김 이름의 로컬 소유권·기록 메타데이터입니다.
2. Hermes를 다시 설치하고 GitHub에 인증한 뒤 같은 플러그인을 설치합니다.
3. `setup`에 **기존과 동일한 Vault 경로·기록 폴더**를 지정합니다.
4. 상태와 기록 인덱스를 확인하고 새 Hermes 세션에서 참조 안내를 확인합니다.

로컬 Hermes 상태가 사라져도 Vault의 ledger를 통해 소유권과 과거 기록 메타데이터를 재연결합니다. 원본 DB가 없는 과거 Markdown 기록도 남깁니다. **Hermes 계정 인증, 원본 세션 DB, 다른 플러그인의 설정까지 복원하는 전체 Hermes 백업은 아닙니다.** Vault/ledger를 모두 지우면 이 패키지로 과거 기록을 복구할 수 없습니다. ledger가 없는 이전 수동 구성은 소유권을 임의로 추정하지 않습니다. 기존 수집기 상태가 있다면 보존한 상태에서 업그레이드해야 합니다.

## 상태·수동 실행·제거

```bash
hermes obsidian-archive status
hermes obsidian-archive uninstall
```

제거 명령은 소유한 예약 작업을 중단/제거하고 안내 플러그인을 비활성화합니다. **Vault·기록·개인 로컬 상태·백업은 삭제하지 않습니다.** 코드 파일까지 삭제하려면 상태를 확인한 뒤 Hermes의 일반 플러그인 제거 기능을 별도로 사용하세요.

수집 상태는 기록 폴더의 `60_Operations/00_Archive-Health.md`에서 마지막 수집 시간과 오류를 확인합니다. 로컬 설정/로그는 기본 Hermes 홈의 `archive-obsidian/`에 있습니다. 수동 실행 시 해당 폴더의 `config.json`과 `source` 값을 사용하세요.

```bash
python "<config.source>/archive.py" --config "<runtime>/config.json"
# 전체 재점검은 --force 추가
```

`<...>`는 설명용 자리표시자입니다. 실제 설정 경로로 바꿔서 실행하세요.

## 업그레이드·사용자 수정 보호

설치된 코드/Skill을 사용자가 수정했다면 기본 설치는 이를 보존합니다. 변경 내용을 검토하고 **해당 패키지 관리 파일에 한해서** 백업 후 교체하려면 `setup`에 `--replace-managed`를 추가합니다. Vault 작성 문서를 교체하는 옵션은 아닙니다. 기존 목적지와 다른 Vault/폴더 지정은 자동 마이그레이션하지 않습니다.

재현 가능한 설치는 GitHub의 정확한 40자리 커밋 SHA로 고정할 수 있습니다.

```bash
hermes plugins install gary3207/Hermes_Plugin_00.Obsidian/source --ref <40자리-커밋-SHA> --enable
```

고정 버전 변경은 실제 커밋을 확인한 뒤 동일 소스 재설치 절차로 진행하세요. 플러그인 코드는 사용자 권한의 Python으로 실행됩니다. 비공개 저장소도 보안 검토를 대신하지 않습니다.

## 개발·검증

유지할 코드/테스트는 모두 `source/`에 있습니다. 개인 설정, Vault, DB, 로그, 백업, 테스트 작업 폴더, Graphify 캐시는 Git에 넣지 않습니다. 게시 파일은 명시적으로 선택합니다.

```bash
python -m unittest discover -s source -p "test_*.py" -v
python source/verify_reinstall.py --bundle source
hermes plugins validate source --json
hermes plugins doctor source --ci
```

유닛 테스트는 실제 예약 작업을 등록하거나 실행하지 않습니다. 실제 예약 작업을 실행하는 **운영 검증**은 `python source/test_archive_task.py --live`이며, 적용 권한이 있는 환경에서만 실행하세요. `--home`, `--no-schedule`, `--no-collect`는 격리된 설치 점검에 사용할 수 있습니다.

`verify_reinstall.py`는 실제 Hermes CLI와 가상 대화 한 건을 사용해 최초 수집 → Hermes 홈 삭제 → 새 홈 설치 → 기존 기록 재연결 → Vault를 보존하는 제거를 검증합니다. 원격 설치를 확인하려면 `--bundle` 대신 `--repo gary3207/Hermes_Plugin_00.Obsidian/source --ref <40자리-커밋-SHA>`를 사용합니다. 실제 사용자 기록이나 예약 작업은 변경하지 않습니다.

## 보안·범위

- DB는 읽기 전용으로 관측합니다. 시스템 메시지·숨김 기록·모델 내부 추론을 원문 내보내기에 포함하지 않습니다.
- exporter/redactor가 없으면 무검열 원문으로 대체하지 않고 실패합니다.
- 기록은 Obsidian Markdown입니다. 과거 텍스트는 실행되지 않는 text 코드블록으로 보존합니다.
- 자동 마스킹이 모든 개인정보/업무 기밀을 제거한다는 보장은 없습니다. Vault의 공유·백업 권한을 점검하세요.
- ledger에도 기록 제목·경로·마스킹된 실패 메타데이터가 있을 수 있습니다. **Vault와 ledger는 개인 데이터이며 Git 배포 대상이 아닙니다.**
- 다른 PC/원격 Gateway/표준 위치 밖 Hermes 홈, 삭제된 원본 DB의 미수집 기록은 기본 수집 대상이 아닙니다.
- 현재 배포 대상은 Windows입니다. Linux/macOS 자동 스케줄러는 제공하지 않습니다.

공식 Hermes 플러그인 안내: https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
