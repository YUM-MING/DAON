"""① 입력 분석: STT 텍스트 → 의미 신호 (규칙 기반, 부정문/단어 경계 고려)"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# --- 위험 ---
SEVERE = ("도와줘", "도와주세요", "살려줘", "살려주", "쓰러졌", "못일어나", "숨이안쉬", "숨을못",
          "숨쉬기힘", "숨쉬기가힘", "숨이막", "의식이")   # 가슴 통증은 CHEST로 따로 판단
# 위급 표현처럼 보이지만 감사/일반 문장인 경우는 판정 전에 제거 ("도와줘서 고마워", "살려주셔서")
NOT_SEVERE = ("도와줘서", "도와주셔서", "살려줘서", "살려주셔서", "도와주신", "살려주신", "도와주셨", "살려주셨")
SEVERE_FUZZY = ("도와줘", "도와주세요", "살려줘", "살려주세요")     # STT 오인식 대비 유사 매칭
# 직접적인 구조 요청 낱말 (회상 질문 안에서도 그대로 위급). SEVERE의 나머지는 위험 증상
RESCUE = ("도와줘", "도와주세요", "살려줘", "살려주")
# 119·구급차를 불러 달라는 현재형 요청 ("119에 전화했어"처럼 지난 일은 제외)
CALL_119 = re.compile(r"(119|일일구|구급차|앰뷸런스)(를|에|좀|빨리)*(불러|전화해|전화좀|연락해|신고해|신고좀)")
# 급하다는 대답 ("급하시면 바로 말씀해 주세요"라고 물은 뒤나 안전 확인 중에만 구조 요청으로 본다: "화장실 급해"는 평소 대화)
# "구해줘": 자신·일행을 구해 달라는 말만 구조 요청 ("물 좀 구해줘", "표 구해줘"는 일반 부탁)
RESCUE_ME = re.compile(r"(나|날|나를|저|절|저를|우리|우릴|우리를)(좀|빨리|제발|어서)*구해(줘|주세요|주십시오|주라|줄래|줘요)")
RESCUE_ME_REPORTED = re.compile(r"(나|날|나를|저|절|저를|우리|우릴|우리를)(좀|빨리|제발|어서)*구해달라")   # 전한 말
RESCUE_BARE = re.compile(r"(제발|누가|누구|빨리|어서|좀)*(구해(줘|주세요|주십시오|줘요))+")   # 대상 없이 "구해줘"만
# 구조 요청 바로 뒤의 인용·가정 표시 ("나 좀 구해줘라고 했어", "…구해줘라고 말하면 어떻게 돼?") → 지금 요청인지 확인
RESCUE_QUOTED = ("라고", "라구", "라는", "란", "라면", "하면", "하고", "같은", "그러", "했")
QUOTE_MARKS = "'\"‘’“”「」"
URGENT = ("급해", "급합니다", "급하다", "위급해", "위급합", "빨리와", "빨리좀와", "큰일났")
# --- 환경 위험 (E: 가스·화재·연기·타는 냄새) — 절 단위로 현재 위험인지 판단 ---
# 위험 낱말과 겹치는 일상 낱말은 먼저 지운다 ("가스비", "가스불", "불편", "불안", "불러", "불고기", 일정 "연기했어")
HAZARD_BENIGN = ("가스비", "가스요금", "가스불", "가스레인지", "가스렌지", "가스점검", "가스검침", "가스밸브", "가스버너",
                 "불편", "불안", "불러", "불고기", "불만", "불쌍", "불교", "불빛", "불면",
                 "연기했", "연기됐", "연기되", "연기하", "연기를", "연기자", "연기력")
HAZARD_PATTERNS = (("gas", re.compile(r"가스(가|도|는|가스)*(냄새|새|샌|샘|샜|누출|유출)")),
                   ("fire", re.compile(r"불이야|불(이|은|도)?(났|난|붙|타)|화재|불길")),
                   ("smoke", re.compile(r"연기(가|도|는)?(나|난|차|찼|자욱|피어|올라|많)")),
                   ("burning", re.compile(r"타는(냄새|것같|거같)|탄내|탄냄새|뭐가타")))
FIRE_KINDS = ("fire", "smoke", "burning")      # 화재 대피 안내를 쓰는 종류 (그 밖은 가스 안내)
HAZARD_NEG = ("안나", "안났", "안난", "나지않", "나진않", "안새", "새지않", "안붙", "안탔", "안타", "없", "아니", "않")
HAZARD_HEDGE = ("것같", "거같", "같기도", "모르겠", "글쎄", "긴가민가", "나나")
HAZARD_COND = re.compile(r"(것같으면|거같으면|으면|면)(?!서)")       # "새면 어떡해", "새는 것 같으면" (가정). "나면서"는 제외
HAZARD_CONTINUE = ("지금", "이제", "다시", "또")                      # "안 나더니 지금은 나" (앞 절의 위험이 지금 있음)
CLAUSE_SPLIT = re.compile(r"[.!?,…~\n]+|(?<=더니)|(?<=는데)|(?<=지만)")
# 환경 위험 비상 중 상황 정보 ("밖으로 나왔어", "불 껐어", "연기 없어졌어"): 기록·응답에만 쓰고 비상을 끝내지 않는다
EVACUATED = ("밖으로나왔", "밖에나왔", "밖으로나갔", "밖에나갔", "밖이야", "밖에있", "대피했", "피했어", "나와있", "나왔어")
HAZARD_GONE = ("껐어", "껐다", "껐습", "꺼졌", "없어졌", "사라졌", "잠갔", "잠궜", "안나", "멈췄", "그쳤")
HELP_WORDS = {"119", "일일구", "구급차", "앰뷸런스"}       # 이 낱말만 말했을 때 → 도움이 필요한지 안전 확인
# 위험 증상 종류 (회상 질문 판단·이번 대화 기록용). 가슴 통증·답답함은 정규식(CHEST, CHEST_TIGHT)으로 따로
DANGER_KINDS = (("breath", ("숨이안쉬", "숨을못", "숨쉬기", "숨이막", "숨이차", "숨차", "숨이안")),
                ("collapse", ("쓰러졌", "의식이", "의식을")),
                ("dizzy", ("어지러", "어지럽")),
                ("bleed", ("피가", "피나")),
                ("injury", ("다쳤",)),
                ("immobile", ("못움직", "못일어나", "일어날수없", "일어날수가없", "일어나지못", "일어설수없",
                              "못일어서", "힘이없")))
# --- STT 오류 (보수적인 규칙만: 안전·증상·구조 요청이 섞이면 적용하지 않는다) ---
HANGUL_THEN_LATIN = re.compile(r"^[가-힣]+[A-Za-z]{2,}$")   # "몇ations": 한글 뒤에 영문이 붙은 낱말 ("TV를"은 영문이 앞)
# whisper가 무음·잡음에서 자주 지어내는 문구와 실제 로그에서 확인된 결과 (INTEGRATION_STATUS: "대대 목욕수수")
HALLUCINATIONS = ("시청해주셔서감사합니다", "구독과좋아요", "구독좋아요", "좋아요와구독", "MBC뉴스", "자막제공",
                  "다음영상에서", "대대목욕수수")
# 로봇이 수행할 수 없는/부적절한 명령으로 들린 말 ("소고기뭇국" → "소리 질러"). 원래 말을 복원하지는 않고 다시 묻는다
SHOUT = re.compile(r"^(크게|좀|다온아|야)*소리(를|좀)*(질러|쳐)(줘|봐|라|요|보세요)?$")
SHOUT_TOKENS = {"질러", "질러줘", "질러봐", "질러라"}
# 비명 감탄사만으로 된 발화 ("악", "아악", "으아아악", "꺄악", "어억"과 그 반복·늘임). 문장부호·공백은 무시.
# "아이고", "어머", "아이쿠", "어이쿠", "에구", "아야"는 해당하지 않는다. 다른 말이 섞이면 이 규칙을 쓰지 않는다
SCREAM = re.compile(r"(?:악|아+악|으+아*악|꺄+아*악|어+억)+")
SCREAM_MAX = 12                     # 이보다 긴 문자열은 비명 단독으로 보지 않는다 (무제한 반복 문자열 제외)
# 신음 감탄사만으로 된 발화 ("윽", "으윽", "끄윽", "끙", "끙끙"과 반복). "음", "으음", "어", "응", "아", "흠"은 해당하지 않는다
MOAN = re.compile(r"(?:으*윽|끄+윽|끙)+")
MOAN_MAX = 8
# --- 작은 일상 의도 (부르기·마이크 확인·다시 말해 달라기): 문장 전체가 이 말일 때만 ---
CALL_WORDS = {"야", "저기요", "저기", "다온아", "다온이", "다온", "여보세요", "얘야", "이봐"}
CALL_PREFIXES = ("다온아", "다온이", "저기요", "야")
MIC_CHECK = {"들려", "들려요", "들리니", "들리나", "들리냐", "들리세요", "들리지", "잘들려", "잘들려요",
             "내말들려", "내말들리니", "내말잘들려", "내말들리지", "내목소리들려", "내목소리잘들려", "내목소리들리니",
             "목소리들려", "목소리잘들려", "제말들려요", "제말들리세요", "제목소리들려요", "제목소리들리세요"}
BARE_HEAR = {"들려", "들려요", "들리니", "들리나", "들리냐", "들리세요", "들리지"}   # 문맥에 따라 다른 뜻일 수 있음
REPEAT_ASK = {"뭐라고", "뭐라고요", "뭐라구", "뭐라구요", "뭐라고했어", "뭐라고했어요", "다시말해줘", "다시말해봐",
              "다시말해주세요", "다시한번말해줘", "다시한번말해주세요", "잘못들었어", "못들었어"}
OK_FUZZY = ("괜찮아", "괜찮아요", "괜찮습니다", "괜찮은데")
# 상대에게 괜찮은지 묻는 말 ("괜찮으세요?", "괜찮으신가요?", "괜찮아지셨어요?", "괜찮은지"): 자기 상태를 말하는 대답이 아니다.
# 높임(-으시-)은 어르신이 자기 상태를 말할 때 쓰지 않으므로 대답으로 보지 않는다 (로봇 질문이 다시 인식돼도 해제되지 않게)
_ASKING = r"괜[찮찬챦](?:아지|으)(?:시|세|신|십|셨|셔)|괜[찮찬챦](?:은지|을까|니|냐)|(?:견딜|참을|버틸)만하(?:시|세|신|십|셨|셔)"
ASKING_OK = re.compile(_ASKING)                      # 낱말 앞부분 ("괜찮으세요" → 대답 아님)
ASKING_OK_WORD = re.compile(f"(?:{_ASKING})[가-힣]*")
# 상대의 증상을 묻는 높임말 ("어디가 불편하세요?", "많이 아프시면", "얼마나 아프세요?"): 어르신 자신의 호소가 아니다
ASKING_SYMPTOM = re.compile(r"(?:아프|아파|불편하|어지러우|어지러|어지럽|다치)(?:시|세|셔|셨|신|십)")
# 듣는 사람에게 묻거나 부탁하는 높임 끝맺음 ("괜찮으세요?", "말씀해 주세요", "알릴게요"): 자기 상태를 말하는 대답이 아니다
ASKS_LISTENER = re.compile(r"(?:세요|셔요|십시오|십니까|시겠어요|시겠습니까|신가요|시나요|셨어요|셨나요|셨죠|드릴게요|드릴까요|알릴게요)$")
MILD = ("아파", "아프", "어지러", "어지럽", "다쳤", "피가", "피나", "못움직", "힘이없",
        "안괜찮", "괜찮지않", "괜찮지가않", "괜찮지못", "숨이차", "숨차", "숨이너무차", "숨이많이차", "숨이좀차",
        "일어날수없", "일어날수가없", "일어나지못", "일어설수없", "못일어서", "못일어나겠")
# --- 일상 통증 vs 위험 증상 ---
# MILD 중 '아파/아프'만 일상 통증 후보. 그 밖의 항목(어지럼·부상·출혈·숨참·못 움직임 등)은 위험 증상으로 즉시 확인
PAIN_WORDS = ("아파", "아프")
MILD_RED = tuple(k for k in MILD if k not in PAIN_WORDS)
# 통증이 심하다는 표현: 있으면 일상 통증으로 보지 않는다. '죽겠네'는 관용 표현이라 넣지 않는다
PAIN_SEVERE = ("너무", "많이", "심하게", "심해", "심한", "엄청", "극심", "죽을것같", "죽을거같", "죽을듯",
               "못참", "참을수없", "참을수가없", "견딜수없", "견딜수가없", "못견디", "견디기힘", "정말", "진짜",
               "계속", "자꾸", "갑자기", "찢어", "깨질", "터질")
# --- 회상 질문 vs 재호소 ---
# "아프다고 했잖아"처럼 이미 말했다며 다시 호소하는 말 (회상 질문 아님)
INSIST = ("잖아", "다니까", "다니깐")
RECALL_ENDS = ("했지", "했나", "했었나", "했었지", "했더라", "했었더라", "했던가", "그랬지", "그랬나", "그랬었나",
               "말했지", "말했나", "그랬더라", "그랬었지", "그랬었더라", "했는지", "그랬는지", "말했더라")
RECALL_WH = ("어디", "뭐가", "무엇이", "무슨", "어느")
SYMPTOM_STEMS = ("아프", "아파", "쑤시", "쑤셔", "저리", "저려", "결리", "결려", "어지럽", "어지러", "불편",
                 "뻐근", "시리", "시려", "욱신")
# '~다고' 없이 증상 바로 뒤에 붙는 구어체 인용: "아프다했더라", "아프다 그랬지", "아프댔지" (증상 낱말 바로 뒤만 본다)
QUOTE_SYMPTOM = re.compile("(?:" + "|".join(SYMPTOM_STEMS) + r")(?:다(?:했|그랬|말했)|댔)")
# 인용 없는 회상 질문: 증상의 과거형 + 기억·회상 묻는 말 ("아까 머리 아팠던 거 기억나?", "아까 어디가 아팠더라?")
PAST_SYMPTOM = ("아팠", "아프던", "쑤셨", "쑤시던", "저렸", "저리던", "결렸", "어지러웠", "어지럽던", "불편했", "불편하던")
RECALL_ASK = ("기억나", "기억해", "기억하", "생각나", "더라", "였지", "였더라", "였나", "였었지")
# 지금 상태가 더 나빠졌다는 말 (절 단위). 뒤에 인용·가정·부정이 붙거나 호전·과거 절이면 악화로 보지 않는다
WORSE = re.compile(r"더심해|더심하|심해졌|심해지|점점심|더(?:아파|아프|어지러|어지럽|힘들|힘드|쑤시|쑤셔|저려|저리|불편)"
                   r"|더안좋|나빠졌|나빠지|악화")
WORSE_AFTER_EXCLUDE = re.compile(r"^(?:지|져|아|어)?(?:면|지않|지는않|진않|않|지마|지말|다고|다했|다그랬|댔)")
WORSE_CLAUSE_EXCLUDE = ("만약", "만일", "덜", "괜찮아졌", "나아졌", "좋아졌", "풀렸")
CURRENT_MARKERS = ("지금", "이제", "점점", "아까보다", "아까는", "방금보다")
# 지금 나아지고 있다는 말 ("덜 아파", "좀 덜해", "나아졌어", "가라앉았어", "괜찮아지는 것 같아"). 부정("안 나아졌어",
# "나아지지 않아", "덜 아프진 않아")·높임 질문("괜찮아지셨어요?")은 제외. 같은 발화의 다른 위험 호소는 그대로 판단한다
IMPROVE = re.compile(r"덜(?:아파|아프|아픈|해|하|쑤시|쑤셔|저려|저리|결려|결리|어지러|어지럽)|나아졌|나아지|나은것같|나은거같"
                     r"|가라앉|좋아졌|한결나|한결낫|괜찮아지|괜찮아졌|풀렸")
IMPROVE_NEG_AFTER = ("지않", "진않", "지는않", "지도않", "셨", "시", "세")
# "덜 아파"의 통증 낱말은 지금의 통증 호소로 세지 않는다 (뒤에 부정이 붙은 "덜 아프진 않아"는 그대로)
LESS_PAIN = re.compile(r"덜(?:아파|아프|아픈|쑤시|쑤셔|저려|저리|결려|결리|어지러|어지럽)(?!지않|진않|지는않|지도않)")
# "괜찮아지는 것 같아", "괜찮아질 거야": 나아지는 중이라는 말이지 괜찮다는 확답은 아니다 ("괜찮아졌어"는 기존대로 확답)
OK_IN_PROGRESS = re.compile(r"괜[찮찬챦]아지(?!셨|시|세)[가-힣]*")
# 증상 종류 → 회상 때 되돌려 말할 표현 (앞에 있을수록 우선)
SYMPTOM_SAID = ((PAIN_WORDS, "아프다고"), (("쑤시", "쑤셔"), "쑤신다고"), (("저리", "저려"), "저리다고"),
                (("결리", "결려"), "결린다고"), (("뻐근",), "뻐근하다고"), (("시리", "시려"), "시리다고"),
                (("욱신",), "욱신거린다고"), (("어지럽", "어지러"), "어지럽다고"))
# 몸 부위 (긴 것 먼저). 한 글자 부위는 '배고파'·'손자'처럼 다른 낱말에 섞이므로 토큰이 부위+조사일 때만 인정
BODY_PARTS = ("허벅지", "옆구리", "엉덩이", "발바닥", "손가락", "발목", "손목", "무릎", "어깨", "머리", "허리",
              "다리", "가슴", "이빨", "치아", "배", "목", "팔", "손", "발", "등", "눈", "귀", "속")
PART_PARTICLES = ("", "이", "가", "은", "는", "도", "를", "을", "쪽", "만", "좀")
NO_ANSWER = {"아니", "아니요", "아뇨", "아니야", "아니에요", "아니오"}
NO_ANSWER_MARKS = ("못참", "못견디", "견딜수없", "견딜수가없", "참을수없", "참을수가없", "안돼")
BEARABLE_MARKS = ("견딜만", "참을만", "괜찮", "버틸만")
NOT_PAIN = ("안아파", "아프지않", "아프지는않", "아프진않", "안아프")
NOT_SYMPTOM = ("피가안나", "피는안나", "피도안나", "피안나", "피가나지않", "피는나지않")   # "피가 안 멈춰"는 위험 유지
# 가슴 통증: 신체 증상이면 위급, 정서적 맥락("그 얘기 들으니")이 분명하면 마음의 아픔으로 본다
CHEST = re.compile(r"가슴이?(?:너무|많이|좀|정말|진짜|자꾸|계속|갑자기)?(?:아파|아프)")
HEART = re.compile(r"마음이?(?:너무|많이|좀|정말|진짜|참)?(?:아파|아프)")
# 흉부 답답함·압박감: 신체 증상이면 안전 확인(mild, '숨이 차'와 같은 수준). 정서 단서 규칙은 가슴 통증과 동일
CHEST_TIGHT = re.compile(r"가슴이?(?:너무|많이|좀|정말|진짜|자꾸|계속|갑자기|꽉)?(?:답답|막히|막힌|막혀|조이|조여|조인|짓눌|눌리|눌려)")
# '얘기/소식' 같은 명사만으로는 정서로 보지 않는다: 듣고·생각하는 등 마음이 움직인 맥락이 있어야 한다
EMOTION_CUES = ("들으니", "들으면", "듣고", "듣는데", "들어보니", "생각하면", "생각만해도", "생각나", "생각이나",
                "보니", "안쓰러", "짠해", "불쌍", "그리워", "보고싶", "돌아가신", "떠난", "서운", "섭섭")
# 갑자기 생긴 통증·하던 일 중의 통증·동반 증상은 정서 단서가 있어도 신체 증상으로 본다 (안전 우선)
PHYSICAL_CUES = ("갑자기", "다가", "하는데", "하는중", "숨", "땀", "쥐어", "조여", "찌르", "뻐근")
# 증상 문자열을 우연히 포함한 일반 단어 ("아파트"의 "아파")
NOT_SYMPTOM_WORDS = ("아파트", "아프리카", "피나는노력")
SELF_HARM = ("죽고싶", "죽어버리", "죽어버릴", "죽을래", "살기싫", "살고싶지않", "사라지고싶", "그만살",
             "목숨을끊", "자살하", "자살할", "자살생각")
NOT_SELF_HARM = ("죽고싶지않", "죽고싶진않", "죽고싶지는않")
# "배고파 죽을래"처럼 상태 표현 바로 뒤의 '죽을래'는 과장 표현 ("힘들어서 죽을래"처럼 이유가 붙으면 위기로 유지)
HYPERBOLE_BEFORE = ("배고파", "힘들어", "졸려", "더워", "추워", "심심해", "지루해", "웃겨", "피곤해", "귀찮아",
                    "바빠", "답답해", "짜증나", "궁금해", "부러워", "배불러", "목말라", "가려워", "무서워", "창피해")
FALL_REPORT = ("넘어졌", "미끄러졌", "쓰러질뻔")       # '떨어졌'은 _fell_off에서 무엇이 떨어졌는지 보고 판단
# "기운이 (좀) 떨어졌어"처럼 주어와 '떨어졌' 사이에 올 수 있는 말
FELL_SKIP = ("좀", "많이", "너무", "확", "다", "조금", "요즘", "갑자기", "또", "완전", "자꾸")
# 말하는 사람 자신이 주어면 '~가'여도 낙상 ("내가 떨어졌어")
FIRST_PERSON = ("내가", "제가", "나는", "저는", "나도", "저도", "난", "전", "나", "저")
# 과거 일을 얘기하는 말이면 지금 넘어진 게 아니므로 낙상 보고로 보지 않는다 ("어제 넘어졌었어")
PAST_MARKERS = ("어제", "그제", "그저께", "지난", "예전", "옛날", "작년", "며칠전", "오래전", "전에")
# 단, 최근 일이면 지금 안전 확인이 필요하다 ("방금 전에", "조금 전에", "10분 전에")
RECENT_MARKERS = ("방금", "조금전", "좀전", "아까", "금방", "분전", "시간전")

# --- 정서 ---
NEGATIVE = ("우울", "외로", "외롭", "쓸쓸", "힘들", "슬퍼", "슬프", "속상", "보고싶", "무서", "무섭",
            "불안", "답답", "허전", "서러", "짜증", "화나", "걱정돼", "걱정되", "걱정이",
            "아무도없", "사람이없", "의지할")
NEGATIVE_FIXED = ("기분이별로", "별로야", "별로네", "별로다", "별로예요", "별로에요", "안좋", "좋지않")
POSITIVE = ("좋아", "좋네", "행복", "기뻐", "즐거", "신나", "재밌", "편안", "든든")
# 함께 기뻐할 만한 분명한 기쁨 표현 ("날씨가 좋네" 같은 약한 긍정은 제외, "안 기뻐"는 부정)
JOY = ("기뻐", "기쁘", "기쁜", "행복", "신나", "신난", "즐거", "기분좋", "기분이좋")
# 부정 표현 앞뒤의 부정어: "안 외로워", "외롭지 않아" → 부정 감정 아님
NEG_AFTER = ("지않", "지는않", "진않", "지도않", "지가않", "하지않", "하지는않", "하진않")

# --- 일상 화제 ---
GREETING = ("안녕", "좋은아침", "굿모닝", "잘잤어")
THANKS = ("고마워", "고맙", "감사")
# 나아졌다는 말: 직전 표정이 슬펐어도 공감으로 되돌리지 않고 평상시 응답 ("안 좋아졌어"처럼 앞의 '안'은 부정)
RECOVERY = ("좋아졌", "나아졌", "괜찮아졌", "풀렸", "편해졌", "가벼워졌")
# 과거형 긍정: "그래도 오늘은 좋았어" — 회복 신호로도 쓰여 슬픈 표정 직후에도 공감으로 되돌리지 않는다
PAST_POSITIVE = ("좋았", "좋더라", "즐거웠", "재밌었", "재미있었", "신났", "행복했", "기뻤")
GOODNIGHT = ("잘자", "잘게", "주무실게", "졸려")
ABOUT_AI = ("너누구", "너는누구", "이름이뭐", "이름이뭔", "너뭐야", "뭐하는로봇", "뭐하는애", "넌누구", "누구세요",
            "이름뭐", "이름은뭐", "이름이머")
OWN_NAME = ("내이름", "제이름", "나의이름", "우리이름")   # 어르신 자신의 이름을 묻는 말 (로봇 이름 질문이 아님)
MEAL = ("밥", "식사", "점심", "저녁", "아침", "배고", "먹었")
# 식사 상태: 무엇을 먹을지 묻는 말 / 아직 안 먹은 말 / 먹었다는 말 (방금 한 말을 되묻지 않기 위해)
MEAL_ASK = ("먹을까", "먹지", "먹을지", "뭐먹", "뭘먹", "뭐해먹")
MEAL_NOT = ("안먹", "못먹", "굶", "배고", "거르", "걸렀")
MEAL_DONE = ("먹었", "식사했", "식사를했")
MEDICINE = ("약먹", "약을먹", "약은먹", "약도먹", "약챙겨", "약을드", "약복용")   # "약 먹었어"는 식사가 아니라 건강 이야기
# 약 이야기 (예약·절약·계약·치약 등은 제외): 약 + 먹기·챙기기·거르기·끊기 같은 복용 표현
# "약 두 개 같이 먹어도 돼?"처럼 사이에 낱말이 조금(4자 이하) 끼어도 본다. "약속", "약간"은 약 이야기가 아님
MED_CONTEXT = re.compile(r"(?<![예절계공요조치규언비])약(?!속|간)[^.?!,]{0,4}?"
                         r"(?:먹|드|챙|복용|걸러|걸렀|거르|빼먹|깜빡|건너|끊|안먹|안드|못먹|못드|잊|까먹|놓)")
# 복용 판단 질문 ("안 먹어도 돼?", "같이 먹어도 돼?", "끊어도 되나?", "안먹어도 되려나?")
MED_ASK = re.compile(r"(?:어도|아도|해도|으면)(?:돼|되|될|괜찮)|되려나|될까|되나|괜찮을까|괜찮나|괜찮은가")
MED_MISSED = re.compile(r"걸렀|걸러|거르|빼먹|깜빡(?!안|하지않)|못먹|안먹|못드|안드|건너뛰|잊어버|잊었|까먹|놓쳤")
MED_REFUSE = ("먹기싫", "먹기귀찮", "먹기가싫", "먹기가귀찮", "안먹을래", "안먹고싶", "먹고싶지않", "끊고싶", "귀찮아", "싫어")
# 고인에 대한 그리움: 그리움 표현 + 고인 표현이 함께 있을 때만 (안전·위기 판정과 무관, 공감 문장만 바뀜)
MISSING = ("보고싶", "그리워", "그립", "생각나", "생각이나")
DECEASED = ("돌아가신", "돌아가셨", "영감", "할멈", "먼저간", "먼저가신", "세상떠", "하늘나라", "떠나보낸")
# 잘 잔다는 말: 잠 이야기를 다시 물은 직후의 대답에만 쓴다 ("잘 자"는 평소엔 잘 자라는 인사)
SLEEP_WELL = ("잘자", "푹자", "잘잔", "잘잤", "푹잤", "잘주무", "푹주무")
# 질문형: 시간 / 날씨(정보가 없으므로 솔직하게) / 로봇이 할 수 있는 것
TIME_ASK = ("몇시", "지금시간", "시간이몇", "시간좀알려", "시간알려")
DATE_ASK = ("며칠", "몇일", "날짜", "무슨요일", "몇요일", "요일이", "몇월")
WEATHER_ASK_MARKS = ("어때", "어떨", "어떻", "알려")         # "날씨" + 이 표현 = 날씨를 묻는 말
ABILITY_ASK = ("뭐할줄", "뭘할줄", "뭐해줄", "뭘해줄", "뭐해줘", "뭘해줘", "할줄알아", "뭐할수있", "뭘할수있")
# 잠을 잘 못 잔다는 말 (되묻지 않고 받아준다. 의료 조언은 하지 않음)
SLEEP_BAD = ("잠을못", "잠을잘못", "못잤", "못자", "잠이안와", "잠이안오", "잠이안와서", "잠을설", "설쳤", "뒤척")
# 피곤함 ("힘들어"는 NEGATIVE라 기존 공감 경로, "힘이 없어"는 MILD라 안전 확인 → 여기서는 다루지 않음)
TIRED = ("피곤", "지쳤", "지친", "지쳐", "기운이없", "기운없", "고단", "나른")
# 이미 말한 불편감 (통증 '아파'는 MILD로 안전 확인 → 여기서는 다루지 않음)
DISCOMFORT = ("쑤셔", "쑤시", "저려", "저리", "결려", "결리", "뻐근", "시려", "시리", "욱신")
BOREDOM = ("심심", "할일없", "지루")
WAIT = ("잠깐", "잠시", "기다려")
INTENSE = ("너무", "정말", "진짜", "많이", "죽겠", "견디기", "계속")
OK_TOKENS = {"응", "네", "예", "그래", "그럼", "살아있어"}   # 단어 단위로만 ("이상하네"의 "네" 오탐 방지)
TOPICS = {
    "lonely": ("외로", "외롭", "쓸쓸", "혼자", "허전", "아무도없", "사람이없"),
    "family": ("아들", "딸", "손주", "손자", "가족", "며느리", "사위", "자식", "보고싶"),
    "health": ("아프", "아파", "허리", "무릎", "다리", "쑤시", "쑤셔", "저려", "약먹", "병원", "혈압", "당뇨"),
    "sleep": ("잠이", "잠을", "잠자", "못잤", "불면", "뒤척"),
    "weather": ("날씨", "추워", "더워", "비가", "눈이"),
}


@dataclass
class TextSignals:
    empty: bool = True
    distress: str = "none"          # none | mild | severe
    self_harm: bool = False         # 극단적 선택 암시
    ok: bool = False                # "괜찮다"는 긍정 응답
    ok_explicit: bool = False       # "괜찮아"처럼 명시적인 안전 응답 ("응/네"만 있으면 False)
    reports_fall: bool = False      # "방금 넘어졌어" 같은 사실 보고
    negative: bool = False
    positive: bool = False
    intense: bool = False           # "너무/정말" 등 강조
    greeting: bool = False
    thanks: bool = False
    goodnight: bool = False
    about_ai: bool = False
    meal: bool = False
    boredom: bool = False
    wait: bool = False
    denies_negative: bool = False   # "안 외로워"처럼 부정 감정을 부인
    recovered: bool = False         # "좋아졌어", "괜찮아졌어"처럼 나아졌다는 말
    joy: bool = False               # "너무 기뻐", "행복해"처럼 분명한 기쁨 표현
    past_positive: bool = False     # "오늘은 좋았어", "재밌었어"처럼 지난 일이 좋았다는 말
    meal_state: str = ""            # ask(뭐 먹을까) | not(안 먹었어·배고파) | done(먹었어) | ""
    discomfort: bool = False        # "무릎이 쑤셔"처럼 이미 말한 불편감
    tired: bool = False             # "피곤하네", "지쳤어"처럼 피곤하다는 말
    medicine: bool = False          # "약 먹었어", "약 먹을 시간이야"
    medicine_state: str = ""        # ask(복용 판단 질문) | missed(복약 누락) | refuse(복용 거부) | "" (그 밖·완료)
    sleep_bad: bool = False         # "잠을 못 자", "잠이 안 와", "잠을 설쳤어"
    sleep_well: bool = False        # "요즘은 잘 자", "푹 잤어" ("잘 못 자"는 아님)
    grief: bool = False             # "돌아가신 영감이 보고 싶네", "할멈이 그리워"
    question: str = ""              # time(몇 시야) | weather(날씨 어때) | abilities(뭐 할 줄 알아) | ""
    topics: list = field(default_factory=list)
    pain_soft: bool = False         # 위험 신호 없는 일상 통증 ("머리가 좀 아프네", "허리 아파서 죽겠네")
    pain_repeat: bool = False       # 같은 증상 반복·재호소로 일상 통증에서 제외됨 (엔진이 문맥을 보고 설정)
    recall_question: bool = False   # "내가 아까 어디가 아프다고 했지?"처럼 지난 말을 묻는 질문
    recall_symptom: bool = False    # 회상 질문이 증상에 관한 것
    insist: bool = False            # "아프다고 했잖아"처럼 이미 말했다며 다시 호소
    symptom_said: str = ""          # 이번 발화의 증상 표현 ("아프다고", "쑤신다고"), 없으면 ""
    symptom_parts: list = field(default_factory=list)   # 증상과 함께 말한 몸 부위 ("머리", "허리")
    answer_no: bool = False         # "아니", "못 참겠어" (견딜 만한지 물었을 때의 부정 대답)
    answer_bearable: bool = False   # "견딜 만해", "괜찮아"
    rescue_like: bool = False       # "알려줘"처럼 구조 요청과 소리만 비슷한 말 (그 자체로는 위급 아님, 문맥으로 판단)
    rescue_word: str = ""           # rescue_like인 낱말 ("알려줘")
    call_help: str = ""             # call_119(119·구급차를 불러 달라는 요청) | hint("119"만 말함) | ""
    danger_kinds: list = field(default_factory=list)   # 위험 증상 종류 (chest·breath·dizzy·…). 회상 질문이면 인용된 것
    stt_unclear: str = ""           # 의미 없는 인식 결과: misheard_command | hallucination | fragment | mixed | repeat | ""
    small_intent: str = ""          # call(야·다온아) | mic(내 목소리 들려?) | mic_bare(들려?) | repeat(뭐라고?) | ""
    hazard_level: str = ""          # 환경 위험: current(지금 위험, 즉시 비상) | uncertain(과거·가정·애매한 부정 → 확인) | ""
    hazard_kinds: list = field(default_factory=list)   # gas | fire | smoke | burning (hazard_level에 해당하는 것)
    urgent: bool = False            # "급해", "빨리 와" (문맥이 있을 때만 구조 요청으로 본다)
    rescue_unsure: bool = False     # "구해줘"가 인용·가정이거나 대상 없이 들림 → 즉시 비상 대신 안전 확인
    hazard_report: str = ""         # evacuated(밖으로 나왔어·대피했어) | gone(불 껐어·연기 없어졌어·냄새 안 나) | ""
    asks_listener: bool = False     # 듣는 사람에게 묻거나 부탁하는 높임말 ("괜찮으세요?", "말씀해 주세요") — 상태 대답 아님
    scream: bool = False            # 비명 감탄사만으로 된 발화 ("아악!", "으아아악!!!") → 안전 확인 (즉시 비상 아님)
    worsening: bool = False         # 지금 상태가 이전보다 나빠졌다는 말 ("지금은 더 심해"). 최근 증상 기록이 있을 때만 확인
    moan: bool = False              # 신음 감탄사만으로 된 발화 ("윽", "끙끙") → 안전 확인 (즉시 비상 아님)
    improving: bool = False         # 지금 나아지고 있다는 말 ("덜 아파", "나아졌어") — 통증 재호소·악화로 보지 않음


def _contains(compact: str, kws, negatable: bool = True) -> bool:
    """키워드 포함 여부. negatable이면 '안 + 키워드', '키워드 + 지 않'은 부정으로 보고 제외한다."""
    for kw in kws:
        start = 0
        while (i := compact.find(kw, start)) >= 0:
            start = i + 1
            if negatable:
                after = compact[i + len(kw):]
                if (i > 0 and compact[i - 1] == "안") or any(after.startswith(n) for n in NEG_AFTER):
                    continue
            return True
    return False


def _jamo(s: str) -> str:
    """한글 음절을 자모로 분해 (오인식 '괜찬아'와 '괜찮아'를 가깝게 보기 위함)"""
    out = []
    for ch in s:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(chr(0x1100 + code // 588))
            out.append(chr(0x1161 + (code % 588) // 28))
            if code % 28:
                out.append(chr(0x11A7 + code % 28))
        else:
            out.append(ch)
    return "".join(out)


def _similar(token: str, targets, threshold: float) -> bool:
    j = _jamo(token)
    return any(difflib.SequenceMatcher(None, j, _jamo(t)).ratio() >= threshold for t in targets)


def _fuzzy_ok(tokens) -> bool:
    for i, t in enumerate(tokens):
        if len(t) < 3 or t[0] in "안못" or (i > 0 and tokens[i - 1] in ("안", "못")):
            continue                              # 부정문은 유사 매칭에서 제외
        if _similar(t, OK_FUZZY, 0.8):
            return True
    return False


def _rescue_me(compact: str, core: str) -> str:
    """'구해줘' 구조 요청 → current(지금 요청, 즉시 비상) | unsure(인용·가정·대상 없음 → 안전 확인) | "".
    바로 뒤에 인용·가정 표시가 붙은 것만 낮춘다. '어제'·'그때' 같은 말이 있다는 것만으로는 낮추지 않는다
    ("어제도 넘어졌는데 지금도 넘어졌어. 나 좀 구해줘!"는 지금 요청)."""
    level = ""
    for m in RESCUE_ME.finditer(compact):
        if compact[m.end():].lstrip(QUOTE_MARKS).startswith(RESCUE_QUOTED):
            level = "unsure"
        else:
            return "current"                      # 인용이 아닌 요청이 하나라도 있으면 지금 요청
    if not level and (RESCUE_ME_REPORTED.search(compact) or RESCUE_BARE.fullmatch(core)):
        level = "unsure"                          # "나 좀 구해달라고 했어", 대상 없는 "구해줘"
    return level


def _has_past(token: str) -> bool:
    """받침 ㅆ이 있는 음절(줬·셨·었·았)이 있으면 과거/완료형 ("도와줬어", "살려주셨어")"""
    return any(0 <= ord(ch) - 0xAC00 < 11172 and (ord(ch) - 0xAC00) % 28 == 20 for ch in token)


_INITIALS = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_PLAIN = {"ㄲ": "ㄱ", "ㄸ": "ㄷ", "ㅃ": "ㅂ", "ㅆ": "ㅅ", "ㅉ": "ㅈ"}   # 된소리는 예사소리와 같게 ("쌀려줘" ≈ "살려줘")


def _head(word: str):
    """첫 음절의 (초성, 중성). 한글이 아니면 None"""
    code = ord(word[0]) - 0xAC00
    if not 0 <= code < 11172:
        return None
    ini = _INITIALS[code // 588]
    return _PLAIN.get(ini, ini), code % 588 // 28


def _fuzzy_rescue(tokens, compact: str = "") -> tuple:
    """구조 요청('도와줘'·'살려줘')의 STT 오인식 판정 → (narrow, broad 낱말).
    narrow: 첫 음절의 초성·모음이 같고 자모 유사도 0.8 이상 ("사려줘", "도와저") → 위급으로 본다.
    broad: 유사도만 높은 말 ("알려줘", "들려주세요", "달려줘") → 그 자체로는 위급이 아니고 문맥(낙상 근거)으로 판단.
    오인식 보정은 현재형 요청에만: 감사("~서")·과거형("~줬어")은 넓히지 않고, "시간/날씨 알려줘"는 정보 질문이다."""
    info_ask = len(tokens) > 1 and _question(compact) in ("time", "weather")
    narrow, broad = False, ""
    for t in tokens:
        if (len(t) < 3 or t.endswith("서") or _has_past(t) or (info_ask and t.startswith("알려"))
                or not _similar(t, SEVERE_FUZZY, 0.8)):
            continue
        if any(_head(t) == _head(s) and _similar(t, (s,), 0.8) for s in SEVERE_FUZZY):
            narrow = True
        elif not broad:
            broad = t
    return narrow, broad


def _stt_unclear(tokens, core: str) -> str:
    """명백한 STT 오류만 (보수적·설명 가능한 규칙). 숫자·영어가 있다는 것만으로는 오류로 보지 않는다."""
    if SHOUT.match(core) or (tokens and all(t in SHOUT_TOKENS for t in tokens)):
        return "misheard_command"
    if any(h in core for h in HALLUCINATIONS):
        return "hallucination"
    if not tokens or all(t.isdigit() for t in tokens):
        return "fragment"                         # 기호·숫자만 ("3 3 3"). "어... 음..." 같은 군말은 기존대로 모호한 대답
    if any(HANGUL_THEN_LATIN.match(t) for t in tokens):
        return "mixed"                            # "몇ations"
    run = best = 1
    for a, b in zip(tokens, tokens[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    if best >= 3 and best / len(tokens) >= 0.6:
        return "repeat"                           # "마킹 마킹 마킹 마킹 마킹" (문장 대부분이 같은 낱말 반복)
    return ""


def _small_intent(core: str) -> str:
    if core in CALL_WORDS:
        return "call"
    rest = core
    for p in CALL_PREFIXES:                       # "다온아 들려?" → "들려"
        if rest.startswith(p) and len(rest) > len(p):
            rest = rest[len(p):]
            break
    if rest in MIC_CHECK:
        return "mic_bare" if rest in BARE_HEAR else "mic"
    if rest in REPEAT_ASK:
        return "repeat"
    return ""


def _hazard(text: str) -> tuple:
    """환경 위험을 절 단위로 판단 → (level, kinds).
    current: 지금 위험("불이 났어", "연기가 나는 것 같아", "안 나더니 지금은 나") — 확신이 없는 말도 현재 위험을 시사하면 current.
    uncertain: 과거("어제 가스 냄새가 났어")·가정("새면 어떡해")·애매한 부정("안 나는 것 같기도 하고") → 확인 질문.
    부정("가스 냄새 안 나")만 있으면 위험 아님."""
    found = {"current": [], "uncertain": []}
    negated_before: list = []
    for clause in CLAUSE_SPLIT.split(text):
        c = re.sub(r"\s+", "", clause)
        for w in HAZARD_BENIGN:
            c = c.replace(w, "|" * len(w))
        hits = [(k, m) for k, p in HAZARD_PATTERNS for m in [p.search(c)] if m]
        if not hits:
            if negated_before and c and any(w in c for w in HAZARD_CONTINUE) and not any(n in c for n in HAZARD_NEG):
                found["current"] += negated_before         # "가스 냄새가 안 나더니 지금은 나!"
            continue
        for kind, m in hits:
            after = c[m.end():m.end() + 6]                  # 위험 낱말 바로 뒤의 부정만 본다 ("불이야, 아무도 없어"는 부정 아님)
            if any(n in after for n in HAZARD_NEG):
                if any(h in c for h in HAZARD_HEDGE):
                    found["uncertain"].append(kind)         # "안 나는 것 같기도 하고…"
                negated_before.append(kind)
            elif ((any(p in c for p in PAST_MARKERS) and not any(r in c for r in RECENT_MARKERS))
                  or HAZARD_COND.search(c[m.start():])):
                found["uncertain"].append(kind)
            else:
                found["current"].append(kind)
    for level in ("current", "uncertain"):
        if found[level]:
            return level, list(dict.fromkeys(found[level]))
    return "", []


def _danger_kinds(view: str, chest_risk: bool) -> list:
    kinds = ["chest"] if chest_risk else []
    return kinds + [k for k, words in DANGER_KINDS if _contains(view, words)]


def _question(compact: str) -> str:
    if any(k in compact for k in DATE_ASK):
        return "date"
    if any(k in compact for k in TIME_ASK):
        return "time"
    if "날씨" in compact and any(k in compact for k in WEATHER_ASK_MARKS):
        return "weather"
    if any(k in compact for k in ABILITY_ASK):
        return "abilities"
    return ""


def _medicine_state(compact: str) -> str:
    """약 이야기의 종류. 복용 판단 질문이 가장 먼저 (누락·거부와 함께 물어도 판단 질문), 다음 거부, 다음 누락.
    약 이야기가 없으면 "" (예약·치약·약속 등은 약 이야기가 아님)"""
    if not (MED_CONTEXT.search(compact) or any(k in compact for k in MEDICINE)):
        return ""
    if MED_ASK.search(compact):
        return "ask"
    if any(k in compact for k in MED_REFUSE):
        return "refuse"
    if MED_MISSED.search(compact):
        return "missed"
    return ""


def _meal_state(compact: str) -> str:
    if any(k in compact for k in MEDICINE) or MED_CONTEXT.search(compact):
        return ""
    if any(k in compact for k in MEAL_ASK):
        return "ask"
    if any(k in compact for k in MEAL_NOT):
        return "not"
    if any(k in compact for k in MEAL_DONE):
        return "done"
    return ""


def _fell_off(tokens) -> bool:
    """'떨어졌'이 사람의 낙상인지: '입맛이/기운이 떨어졌어'처럼 주어(~이/가) 바로 뒤면 상태 변화로 본다.
    '침대에서 떨어졌어', '방금 떨어졌어', '떨어졌어'(단독)는 낙상으로 본다. '안 떨어졌어'는 부정."""
    for i, t in enumerate(tokens):
        if not t.startswith("떨어졌"):
            continue
        j = i - 1
        while j >= 0 and tokens[j] in FELL_SKIP:
            j -= 1
        prev = tokens[j] if j >= 0 else ""
        if prev == "안" or (prev not in FIRST_PERSON and prev.endswith(("이", "가"))):
            continue
        return True
    return False


def _body_parts(tokens) -> list:
    parts = []
    for t in tokens:
        for p in BODY_PARTS:
            rest = t[len(p):]
            if t.startswith(p) and (rest in PART_PARTICLES or rest.startswith(SYMPTOM_STEMS)):
                if p not in parts:
                    parts.append(p)
                break
    return parts


def _improving(text: str) -> bool:
    """지금 증상이 나아지고 있다는 말이 있는 절이 있는지 (부정·높임 질문 제외)"""
    for clause in CLAUSE_SPLIT.split(text):
        c = re.sub(r"\s+", "", clause or "")
        for m in IMPROVE.finditer(c):
            before, after = c[:m.start()], c[m.end():]
            if before.endswith(("안", "못")) or after.startswith(IMPROVE_NEG_AFTER) or "아니" in after:
                continue
            return True
    return False


def _recall_noquote(compact: str) -> bool:
    """인용('~다고') 없이 지난 증상을 기억하는지 묻는 말"""
    return (any(k in compact for k in PAST_SYMPTOM) and any(k in compact for k in RECALL_ASK)
            and not any(k in compact for k in INSIST))


def _recall_clause(clause: str) -> bool:
    c = re.sub(r"\s+", "", clause)
    return _recall_question(clause, c) or _recall_noquote(c)


def _worsening(text: str) -> bool:
    """지금 상태가 이전보다 나빠졌다는 호소가 있는 절이 있는지 ("지금은 더 심해", "점점 심해지고 있어").
    인용("더 심하다고 했었나")·가정("더 심해지면")·부정("더 아프지 않았으면")·호전("덜", "나아졌")·
    과거 절("작년엔 …") ·회상 질문 절은 제외한다"""
    for clause in CLAUSE_SPLIT.split(text):
        c = re.sub(r"\s+", "", clause or "")
        if not c or any(k in c for k in WORSE_CLAUSE_EXCLUDE) or _recall_clause(clause):
            continue
        if any(p in c for p in PAST_MARKERS) and not any(k in c for k in CURRENT_MARKERS):
            continue
        for m in WORSE.finditer(c):
            if not WORSE_AFTER_EXCLUDE.match(c[m.end():]):
                return True
    return False


def _current_complaint(text: str) -> bool:
    """회상 질문이 아닌 절에 지금의 증상·위험·악화 호소가 있는지 (회상 질문과 함께 말해도 무시하지 않기 위해)"""
    for clause in CLAUSE_SPLIT.split(text):
        c = re.sub(r"\s+", "", clause or "")
        if not c or _recall_clause(clause):
            continue
        if _contains(c, SEVERE) or _contains(c, MILD) or _aches(c, CHEST) or _worsening(clause):
            return True
    return False


def _recall_question(text: str, compact: str) -> bool:
    """지난 말을 되묻는 질문: 인용('~다고') + 질문 표현(했지·했나·어디·뭐가·물음표). '했잖아'는 재호소"""
    if ("다고" not in compact and not QUOTE_SYMPTOM.search(compact)) or any(k in compact for k in INSIST):
        return False
    return (any(k in compact for k in RECALL_ENDS) or any(k in compact for k in RECALL_WH)
            or text.rstrip().endswith("?"))


def _aches(view: str, pattern) -> list:
    """'가슴이 (너무) 아파' 같은 표현의 위치. '아프지 않아'처럼 뒤에서 부정되면 제외"""
    return [m.span() for m in pattern.finditer(view)
            if not any(view[m.end():].startswith(n) for n in NEG_AFTER)]


def _blank(view: str, spans) -> str:
    for a, b in spans:
        view = view[:a] + "|" * (b - a) + view[b:]
    return view


def analyze(text: str) -> TextSignals:
    text = (text or "").strip()
    if not text:
        return TextSignals()
    compact = re.sub(r"\s+", "", text)
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", text)

    word_view = compact
    for p in NOT_SYMPTOM_WORDS:
        word_view = word_view.replace(p, "|" * len(p))
    chest, heart, tight = _aches(word_view, CHEST), _aches(word_view, HEART), _aches(word_view, CHEST_TIGHT)
    emotional_cue = any(c in compact for c in EMOTION_CUES) and not any(c in compact for c in PHYSICAL_CUES)
    emotional_chest = bool(chest) and emotional_cue
    chest_tight = bool(tight) and not emotional_cue       # 정서 맥락이 아닌 흉부 답답함·압박감
    heartache = bool(heart) or emotional_chest or (bool(tight) and emotional_cue)  # 위험이 아니라 부정 감정
    emotional_view = _blank(word_view, heart + (chest if emotional_chest else []))
    pain_view = ASKING_SYMPTOM.sub("|", emotional_view)    # "아프세요?"는 상대에게 묻는 말 (로봇 질문이 다시 인식된 경우 포함)
    pain_view = LESS_PAIN.sub(lambda m: "|" * len(m.group()), pain_view)   # "덜 아파"는 호전이지 통증 호소가 아니다
    for p in NOT_PAIN + NOT_SEVERE + NOT_SYMPTOM:
        pain_view = pain_view.replace(p, "")
    harm_view = compact
    for p in NOT_SELF_HARM + tuple(h + "죽을래" for h in HYPERBOLE_BEFORE):
        harm_view = harm_view.replace(p, "")

    self_harm = any(k in harm_view for k in SELF_HARM)
    if (chest and not emotional_chest) or _contains(pain_view, SEVERE):
        distress = "severe"
    elif chest_tight or _contains(pain_view, MILD):       # "안 다쳤어", "어지럽지 않아"는 부정으로 제외
        distress = "mild"
    else:
        distress = "none"

    narrow, rescue_word = _fuzzy_rescue(tokens, compact)
    if distress == "none" and not self_harm and narrow:
        distress = "severe"                       # "도와저" 같은 오인식도 위급으로 본다
    core = re.sub(r"[^가-힣A-Za-z0-9]", "", text)
    call_help = "call_119" if CALL_119.search(core) else "hint" if core in HELP_WORDS else ""
    if call_help == "call_119" and not self_harm:
        distress = "severe"                       # 119·구급차를 불러 달라는 말은 구조 요청
    rescue_me = _rescue_me(compact, core)
    if rescue_me == "current" and not self_harm:
        distress = "severe"                       # "나 좀 구해줘": 자신·일행의 구조 요청

    # 회상 질문("아까 어디가 아프다고 했지?"): 인용된 증상은 지금의 호소가 아니다.
    # 단, "도와줘"·"살려줘" 같은 직접적인 구조 요청과 극단적 선택 암시는 그대로 위급으로 본다.
    # 인용된 위험 증상(가슴·숨·어지럼 등)은 danger_kinds로 넘겨 엔진이 이번 대화 기록·안전 상태로 판단한다
    # 회상 질문: 인용형("아프다고 했지") 또는 인용 없는 과거형("아팠던 거 기억나?"). 같은 발화의 다른 절에
    # 지금의 증상·위험·악화 호소가 있으면 순수 회상이 아니다 ("아까 어디 아팠는지 기억나? 그런데 지금은 더 아파")
    recall = ((_recall_question(text, compact) or _recall_noquote(compact))
              and not _current_complaint(text))
    worsening = _worsening(text)
    danger_kinds = _danger_kinds(pain_view, (bool(chest) and not emotional_chest) or chest_tight)
    if recall and not self_harm:
        distress = "severe" if (_contains(pain_view, RESCUE) or call_help == "call_119"
                                or rescue_me == "current") else "none"
    # 환경 위험은 일반 대화·STT 오류·통증보다 먼저: 지금 위험이면 위급 발화와 같이 즉시 비상
    hazard_level, hazard_kinds = _hazard(text)
    if hazard_level == "current" and recall:
        hazard_level = "uncertain"                # "아까 불났다고 했지?": 회상 질문이면 지금 상황을 확인한다
    if hazard_level == "current":
        distress = "severe"
    insist = any(k in compact for k in INSIST) and any(k in compact for k in SYMPTOM_STEMS)
    reports_fall = (_contains(compact, FALL_REPORT) or _fell_off(tokens)) and (   # "안 넘어졌어"는 부정
        any(k in compact for k in RECENT_MARKERS) or not any(k in compact for k in PAST_MARKERS))
    # 같은 통증 낱말을 거듭 말함 ("아파 아파 아파"): 심한 통증 표현으로 본다
    pain_repeated = sum(1 for t in tokens if t.startswith(PAIN_WORDS)) >= 2
    # 일상 통증: 통증 표현만 있고 위험 증상·심한 정도·재호소·낙상 언급이 없을 때
    pain_soft = (distress == "mild" and not chest_tight and _contains(pain_view, PAIN_WORDS)
                 and not _contains(pain_view, MILD_RED) and not any(k in compact for k in PAIN_SEVERE)
                 and not insist and not reports_fall and not pain_repeated)
    symptom_said = "" if recall else next(
        (said for stems, said in SYMPTOM_SAID if _contains(pain_view, stems)), "")

    ok_tokens = [t for t in tokens if not ASKING_OK_WORD.fullmatch(t)    # "괜찮으세요?"는 대답이 아니다
                 and not OK_IN_PROGRESS.fullmatch(t)]                      # "괜찮아지는 것 같아"는 확답이 아니다
    ok_compact = OK_IN_PROGRESS.sub("", ASKING_OK.sub("", compact))
    explicit_ok = (any(t.startswith("괜찮") for t in ok_tokens)
                   or "이상없" in ok_compact or "문제없" in ok_compact
                   or _fuzzy_ok(ok_tokens))
    ok = distress == "none" and not self_harm and (any(t in OK_TOKENS for t in tokens) or explicit_ok)
    rescue_like = bool(rescue_word) and distress == "none" and not self_harm and not recall
    small_intent = _small_intent(core)
    scream = (0 < len(core) <= SCREAM_MAX and bool(SCREAM.fullmatch(core)) and distress == "none"
              and not self_harm)            # 비명 단독 (다른 말·위험 표현이 섞이면 기존 판단)
    moan = (0 < len(core) <= MOAN_MAX and bool(MOAN.fullmatch(core)) and distress == "none"
            and not self_harm)              # 신음 단독
    # STT 오류 판정은 안전 신호가 하나도 없을 때만 (구조 요청·증상·낙상 언급·긍정 응답·작은 의도가 우선)
    stt_unclear = "" if (distress != "none" or self_harm or reports_fall or symptom_said or rescue_like or ok
                         or recall or call_help or small_intent or danger_kinds or hazard_level
                         or rescue_me or scream or moan) \
        else _stt_unclear(tokens, core)

    negative = (_contains(compact, NEGATIVE) or _contains(compact, NEGATIVE_FIXED, negatable=False)
                or heartache)
    past_positive = (not negative) and _contains(compact, PAST_POSITIVE)
    positive = (not negative) and (_contains(compact, POSITIVE) or past_positive)
    denies = (not negative) and any(k in compact for k in NEGATIVE) and not _contains(compact, NEGATIVE)
    return TextSignals(
        empty=False, distress=distress, self_harm=self_harm, ok=ok, ok_explicit=ok and explicit_ok,
        reports_fall=reports_fall,
        negative=negative, positive=positive,
        intense=any(k in compact for k in INTENSE),
        greeting=any(k in compact for k in GREETING),
        thanks=any(k in compact for k in THANKS),
        recovered=(not negative) and (_contains(compact, RECOVERY) or past_positive),
        past_positive=past_positive,
        joy=(not negative) and _contains(compact, JOY),
        meal_state=_meal_state(compact),
        discomfort=_contains(compact, DISCOMFORT),
        tired=_contains(compact, TIRED),
        medicine=any(k in compact for k in MEDICINE) or bool(MED_CONTEXT.search(compact)),
        medicine_state=_medicine_state(compact),
        sleep_bad=any(k in compact for k in SLEEP_BAD),
        question=_question(compact),
        sleep_well=any(k in compact for k in SLEEP_WELL),
        grief=any(k in compact for k in MISSING) and any(k in compact for k in DECEASED),
        goodnight=any(k in compact for k in GOODNIGHT),
        about_ai=any(k in compact for k in ABOUT_AI) and not any(k in compact for k in OWN_NAME),
        meal=any(k in compact for k in MEAL),
        boredom=any(k in compact for k in BOREDOM),
        wait=any(k in compact for k in WAIT), denies_negative=denies,
        topics=[n for n, kws in TOPICS.items() if any(k in emotional_view for k in kws)],   # '마음이 아파'는 건강 화제 아님
        pain_soft=pain_soft, recall_question=recall,
        recall_symptom=recall and (any(k in compact for k in SYMPTOM_STEMS + PAST_SYMPTOM) or "어디" in compact),
        insist=insist, symptom_said=symptom_said,
        symptom_parts=_body_parts(tokens) if symptom_said else [],
        answer_no=bool(NO_ANSWER & set(tokens)) or any(k in compact for k in NO_ANSWER_MARKS),
        answer_bearable=any(k in ok_compact for k in BEARABLE_MARKS),
        rescue_like=rescue_like, rescue_word=rescue_word if rescue_like else "", call_help=call_help,
        danger_kinds=danger_kinds, stt_unclear=stt_unclear, small_intent=small_intent,
        hazard_level=hazard_level, hazard_kinds=hazard_kinds, urgent=any(k in core for k in URGENT),
        rescue_unsure=rescue_me == "unsure" and distress != "severe" and not self_harm,
        hazard_report="evacuated" if any(k in compact for k in EVACUATED)
        else "gone" if any(k in compact for k in HAZARD_GONE) else "",
        asks_listener=any(ASKS_LISTENER.search(t) for t in tokens),
        scream=scream, worsening=worsening, moan=moan, improving=_improving(text) and not worsening)


def robot_speech_flags(text: str) -> tuple:
    """로봇이 말할 문장이 다시 인식됐을 때 안전 판단을 바꿀 수 있는지 (4·5-A 로봇 문장 검사와 같은 기준).
    trigger 태그: severe·self_harm·call_help·hazard·rescue_like·urgent·stt_unclear·rescue_unsure
    'ok': 괜찮다는 대답으로 판정됨 (ok·ok_explicit·answer_bearable·recovered), 'mild': 가벼운 위험 발화로 판정됨"""
    s = analyze(text)
    tags = [n for n, on in (("severe", s.distress == "severe"), ("self_harm", s.self_harm),
                            ("call_help", s.call_help), ("hazard", s.hazard_level),
                            ("rescue_like", s.rescue_like), ("urgent", s.urgent), ("stt_unclear", s.stt_unclear),
                            ("rescue_unsure", s.rescue_unsure), ("scream", s.scream), ("moan", s.moan)) if on]
    if s.ok or s.ok_explicit or s.answer_bearable or s.recovered:
        tags.append("ok")
    if s.distress == "mild":
        tags.append("mild")
    return tuple(tags)
