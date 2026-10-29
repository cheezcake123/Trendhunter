# TrendHunter

바이낸스 자동매매 봇. 소스는 `trendhunter/`, 서버 배포본은 `trendhunter.zip`.
모든 작업은 아래 규칙을 따른다.

## 규칙

- 서버(우분투, AWS Lightsail 서울)가 https://github.com/cheezcake123/Trendhunter/raw/main/trendhunter.zip 을 받아서 텔레그램 `/update`로 설치한다. 그래서 수정이 끝나면 반드시 `trendhunter.zip`을 다시 만들어 같은 이름으로 덮어쓴다. zip 안의 최상위 폴더는 `trendhunter/` 이다.
- 파이썬 표준 라이브러리만 쓴다(pip 패키지 금지). python3 3.8 이상에서 돌아가야 한다.
- 수정할 때마다 `config.json`의 `version`을 올린다.
- 새 파일을 추가하면 `updater.py`의 `REQUIRED` 목록에도 넣는다. 새로 넣은 설정 키는 `bot.py --check` 에서 검사한다.
- API 키·텔레그램 토큰 같은 비밀정보는 절대 커밋하지 않는다. 비밀정보는 서버의 `secrets.json`에만 있다.
- 돈과 관련된 안전 규칙은 깨지 않는다:
  - 모의(paper) 모드에서는 실제 주문·이체를 하지 않는다.
  - 선물 포지션마다 거래소 손절 주문이 정확히 1개 있어야 한다.
  - 결과를 모르는 진입 주문은 다시 보내지 않는다.
  - 출금 기능은 만들지 않는다.
- 커밋하기 전에 테스트를 전부 통과시킨다. 기능을 고치거나 추가하면 `tests/`에 테스트도 함께 추가한다.
- 텔레그램 메시지와 설명은 한국어로 쓴다.

## 작업 메모

- 테스트: `cd trendhunter && python3 -m unittest discover -s tests -t .`
  - Windows에서는 `python -X utf8 -m unittest discover -s tests -t .` 로 돌린다. 이때 `test_install`(bash·systemd 필요), `test_update_and_rollback`(`file://C:\...` URL), `test_process_lifecycle`(SIGTERM이 강제 종료가 됨)은 OS 차이로 실패하므로 우분투에서 확인한다.
  - pandas 비교 테스트 6개는 pandas가 없으면 건너뛴다(정상).
- zip 다시 만들기(저장소 루트에서, `__pycache__` 제외):
  `python3 -c "import os,zipfile; z=zipfile.ZipFile('trendhunter.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(b,f)) for b,d,fs in sorted(os.walk('trendhunter')) if '__pycache__' not in b for f in sorted(fs)]; z.close()"`
- 줄바꿈은 LF만 쓴다(`.gitattributes`로 고정). CRLF가 섞인 `install.sh`·`th`는 서버에서 실행되지 않는다.
