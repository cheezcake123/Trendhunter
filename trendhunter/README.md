# TrendHunter v2.0.0

바이낸스 자동매매 봇. 서버(우분투)에서 24시간 돌고, 텔레그램으로 조작합니다.
돈을 두 바구니로 나눠 운용합니다.

- **현물 50%**: 거래대금 상위 6개 메이저 코인의 추세 모멘텀(20·60·120·250일) + 변동성 맞춤 비중. 매일 09:05 KST 한 번 리밸런싱. 레버리지 없음.
- **선물 50%**: 일봉 정배열 + 4시간봉 20봉 고점 돌파 롱, 거래소 손절(Algo Order) + 추적 손절.
- 두 지갑 비율은 범용 이체로 자동 유지 (목표에서 10%p 이상 벗어나면, 매월 1일에는 2%p 이상이면).

자세한 설치·운영·전략 설명과 전략 비교 결과는 Claude가 함께 만든 설명서를 보세요.

## 설치 (서버에서 한 번)
```
U=https://github.com/<아이디>/<저장소>/raw/main/trendhunter.zip
cd ~ && curl -fsSL -o th.zip "$U" && python3 -m zipfile -e th.zip ~ && bash ~/trendhunter/install.sh "$U"
```
- 파이썬 표준 라이브러리만 사용(추가 설치 없음), Ubuntu 22.04/24.04, python3 3.8+
- 키는 `~/trendhunter-data/secrets.json` (권한 600)에만 저장됩니다. 코드에는 비밀정보가 없습니다.
- 처음엔 모의(PAPER) 모드로 시작합니다. 실전 전환은 텔레그램 `/live`.
- API 키 권한: Enable Futures, Enable Spot & Margin Trading, Permits Universal Transfer. **출금 권한은 끄고**, 서버 IP만 허용.

## v1 → v2 업데이트
1. 이 zip을 GitHub 저장소에 같은 이름으로 덮어 올리기
2. 텔레그램 `/update` (모의 모드는 바로 현물까지 동작)
3. 실전 전: API 키에 현물 거래·범용 이체 권한 추가 → `/selftest` → `/selftest live` → `/live`

## 파일
| 파일 | 역할 |
|---|---|
| bot.py | 메인 루프, 텔레그램 명령, 브리핑, 현물/선물 배분·이체 |
| strategy.py | 선물: 일봉 4이평 정배열 + 4시간봉 20봉 돌파, 손절·추적 규칙 |
| spot_strategy.py | 현물: 모멘텀 신호, 변동성 비중, 거래대금 순위 종목 선정, 매매 계획, 순수 파이썬 백테스트 |
| spot.py | 현물 실전·모의 체결, 일일 리밸런싱, 평가 |
| risk.py | 선물 1회 위험 기반 수량·레버리지, 가드(일일한도·쿨다운·연패휴식·바닥선) |
| brokers.py | 선물 실전(바이낸스)·모의 체결 |
| binance_api.py, binance_spot.py | 바이낸스 선물·현물 REST 클라이언트 |
| backtest.py, backtest_mix.py | 선물 백테스트, 현물+선물 합산 백테스트 (`/backtest 730`) |
| selftest.py | 계정·API 권한 점검, 소액 실주문 왕복 테스트(선물·현물·이체) |
| updater.py | zip 주소에서 업데이트·롤백 (검증 후 교체) |
| install.sh, th | 설치 스크립트, 서버 관리 명령 |
| config.json | 기본 설정 (텔레그램 `/set`·`/alloc`·`/preset` 변경값은 data/settings.json 에 저장) |
| tests/ | 가짜 거래소(현물·선물·이체 포함)로 돌리는 테스트 (`python3 -m unittest discover -s tests -t .`) |

## 텔레그램 명령 (추가분)
`/spot` 현물 보유·목표 비중 · `/alloc 50` 현물 비중 % · `/preset 안정|기본|공격` · `/export [연도]` 거래기록 CSV

현물 지갑의 USDT와 후보 코인(spot_candidates)은 봇이 전부 운용합니다. 따로 보관할 코인은 다른 곳에 두세요.
과거 성과는 미래 수익을 보장하지 않습니다. 잃어도 되는 돈으로만 운용하세요.
