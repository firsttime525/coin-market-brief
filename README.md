# 코인 시황 브리핑 봇

매일 **06시 · 18시 · 23시 (KST)** 시장 전체 상태를 요약해서 텔레그램으로 보냅니다.
기존 후보 알림 봇(coinalyze-alert)과는 완전히 별개의 봇·저장소입니다.

## 담기는 내용
- 종합 판단 (규칙 기반 점수 + 주의 경고)
- 비트코인: 가격, 7일 등락, 50·200일선
- TOTAL / TOTAL2 / TOTAL3, USDT.D, BTC.D, ETH/BTC (직전 브리핑 대비 변화)
- 펀딩비 · 미결제약정(OI), 공포·탐욕 지수
- DXY, 나스닥, 미국 10년물 금리
- 36시간 이내 미국 고중요도 경제 일정 + 다음 FOMC D-day
- 직전 브리핑 이후 코인 뉴스 헤드라인

## 설정 방법 (10분)
1. **새 봇 만들기**: 텔레그램 @BotFather → `/newbot` → 이름 정하기 → 토큰 복사
2. **채팅 ID 확인**: 새 봇에게 아무 메시지나 보낸 뒤 브라우저에서
   `https://api.telegram.org/bot<토큰>/getUpdates` 열기 → `"chat":{"id": 숫자}` 의 숫자
   (채널로 받으려면 봇을 채널 관리자로 추가하고 채널에 글을 하나 쓴 뒤 확인, `-100…` 형태)
3. **GitHub 새 저장소** 만들기 (예: `coin-market-brief`, 비공개 추천) → 이 폴더의 파일을 그대로 업로드
   (`.github/workflows/market-brief.yml` 경로가 유지되어야 합니다)
4. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
   - `BRIEF_BOT_TOKEN` : 1번 토큰
   - `BRIEF_CHAT_ID` : 2번 숫자
   - `COINALYZE_API_KEY` : (선택) 기존 봇에서 쓰던 키. 넣으면 OI까지 나옵니다
5. **Actions 탭 → 시황 브리핑 → Run workflow** 로 바로 테스트

## 참고
- GitHub 예약 실행은 몰릴 때 10~20분 늦어질 수 있어 05:50 / 17:50 / 22:50에 걸어 두었습니다.
  정확한 시간이 더 중요하면 workflow 파일의 cron 분(50)을 바꾸세요.
- FOMC 날짜는 `market_brief.py` 상단 `FOMC_DECISIONS_ET`에 2026년분이 들어 있습니다. 2027년 일정이 나오면 추가하세요.
- 데이터 소스 하나가 막혀도 나머지는 그대로 전송됩니다. (해당 칸에 "데이터 없음")
- 저장소가 60일간 커밋이 없으면 GitHub가 예약 실행을 멈출 수 있습니다. 메일이 오면 버튼 한 번으로 다시 켜면 됩니다.
