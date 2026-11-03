"""A small invented work for dry runs and the real-model smoke test: raw export plus three manuscripts.

Every name, sentence and value here is synthetic. The target (episode 3, about 1,500 code points) contains
statements that agree with, contradict or need context against the invented settings, so the planner and the
judge have something real to do.
"""

from hashlib import sha256
from pathlib import Path

from scripts.validation_experiment.common import write_json

WORK_ID = "00000000-0000-4000-8000-0000000a0001"
TARGET_NO = 3

EPISODES = {
    1: ("001화 합성 첫날.txt", """국경 마을 바렌에 첫눈이 내린 날, 검사 한서윤은 산적과 싸우다 왼팔을 잃었다.
마법사 도겸은 불 마법으로 산적들을 쫓아냈지만, 마법을 쓴 뒤에는 한참 동안 기침을 멈추지 못했다.
"불을 쓰면 늘 이렇게 숨이 막혀." 도겸이 쉰 목소리로 말했다.
마을 사람들은 회색탑 근처로는 가지 말라고 했다. 회색탑 안에서는 어떤 마법도 일어나지 않는다고 했다.
은빛 강은 마을 동쪽을 흐른다. 밤에 은빛 강을 건넌 사람은 돌아오지 못한다는 금기가 전해졌다.
"""),
    2: ("002화 합성 대장간.txt", """대장장이 바르탄은 쉰한 살이었다. 그는 한서윤을 위해 강철 의수를 만들었다.
"이 의수로 검을 쥘 수는 있소. 다만 비가 오면 관절이 굳어서 느려질 거요." 바르탄이 말했다.
한서윤은 의수를 끼고 처음으로 양손으로 검을 쥐어 보았다. 손끝이 조금 떨렸다.
도겸은 대장간 화로에 불 마법을 쓰다가 또 기침을 했다.
바르탄은 회색탑의 문이 해가 진 뒤에는 열리지 않는다고 일러 주었다.
"""),
    3: ("003화 합성 회색탑.txt", """비가 그치지 않는 밤이었다. 한서윤은 젖은 망토를 여미고 회색탑 앞에 섰다.
강철 의수의 관절이 빗물에 차갑게 식어 있었다. 그런데도 그녀는 양손으로 검을 쥐고 단숨에 세 번을 베었다.
의수는 조금도 느려지지 않았다. 마치 맑은 날처럼 가볍게 움직였다.
"오늘은 의수가 이상하게 잘 움직이네." 서윤이 중얼거렸다.
도겸이 뒤따라와 손바닥 위에 불꽃을 피웠다. 그 불빛으로 둘은 회색탑의 문을 비추었다.
해가 진 지 오래였지만 회색탑의 문은 반쯤 열려 있었다.
둘은 탑 안으로 들어갔다. 도겸은 탑 안의 계단에서 다시 불 마법을 써 앞을 밝혔다.
불꽃은 탑 안에서도 또렷하게 타올랐고, 도겸은 기침 한 번 하지 않았다.
"이상하군. 오늘은 숨이 하나도 막히지 않아." 도겸이 말했다.
탑의 꼭대기에는 바르탄이 남긴 쪽지가 있었다. 쪽지에는 이렇게 적혀 있었다.
"의수의 관절에 기름을 새로 먹였소. 이제 비가 와도 굳지 않을 거요."
서윤은 쪽지를 접어 품에 넣었다. 바르탄은 올해 마흔 살이 되었다고 했던 말이 떠올랐다.
그날 밤, 둘은 은빛 강을 건너 동쪽 숲으로 향했다. 강물은 달빛을 받아 하얗게 빛났다.
강을 건너는 동안 아무 일도 일어나지 않았다. 둘은 숲 가장자리에서 야영을 했다.
도겸은 모닥불에 불 마법으로 불을 붙였고, 이번에는 짧게 기침을 했다.
서윤은 의수로 검집을 쥐고 잠들었다. 멀리서 회색탑의 종이 한 번 울렸다.
다음 날 아침, 서윤은 오른손만으로 아침을 준비했다. 의수는 밤새 차가워져 있었다.
도겸은 "탑 안에서 마법이 된 건 처음 봐."라고 말하며 지도를 펼쳤다.
지도에는 은빛 강 너머의 마을 이름이 적혀 있지 않았다.
"바르탄이 이 지도를 줬을 때 뭐라고 했지?" 서윤이 물었다.
"강을 건너면 북쪽으로 사흘을 걸으라고 했어. 그런데 그 사람은 이 강을 건너 본 적이 없대." 도겸이 대답했다.
서윤은 고개를 끄덕이고 검을 등에 멨다. 오른팔에는 아직 산적에게 맞은 멍이 남아 있었다.
숲길은 생각보다 좁았다. 둘은 한 줄로 걸었고, 서윤이 앞에서 덤불을 베어 길을 냈다.
점심 무렵 비가 다시 내리기 시작했다. 의수의 관절에서 삐걱거리는 소리가 났지만 움직임은 여전히 부드러웠다.
도겸은 젖은 장작에 불 마법을 쓰려다 그만두었다. "비가 오면 내 불은 금방 꺼져. 오늘은 쉬자."
그날 저녁, 둘은 작은 오두막을 발견했다. 오두막 문에는 바렌 마을의 문장이 새겨져 있었다.
안에는 오래된 일지가 있었다. 일지의 마지막 장에는 "회색탑의 종이 두 번 울리면 탑 안의 마법이 깨어난다"고 적혀 있었다.
서윤은 일지를 덮었다. 어젯밤 들었던 종소리는 분명히 한 번뿐이었다.
도겸은 손바닥을 내려다보았다. "그럼 어젯밤 탑 안에서 내 불이 탄 건 뭐였을까."
대답할 사람은 없었다. 빗소리만 오두막 지붕을 두드렸다.
"""),
}


def raw_export(texts):
    episodes = [{"id": f"00000000-0000-4000-8000-0000000b000{no}", "episodeNo": no, "status": "ANALYZED",
                 "contentHash": sha256(texts[no].encode()).hexdigest(), "contentS3Key": f"synthetic/episode-{no}.txt",
                 "contentS3Version": "synthetic-v1", "charCount": len(texts[no])} for no in (1, 2)]
    seoyun, dogyeom, bartan = (f"00000000-0000-4000-8000-0000000c000{n}" for n in (1, 2, 3))
    envelope = {"__catchhole_snapshot": {"format": "character-snapshot-entry", "version": 1,
                                         "factValue": "강철 의수(비가 오면 관절이 굳어 느려짐)",
                                         "valueJson": {"value": "강철 의수"}}}
    return {
        "exportVersion": 1, "exportedAt": "2026-11-02T00:00:00Z", "workId": WORK_ID, "targetEpisodeNo": TARGET_NO,
        "episodes": episodes,
        "characters": [
            {"id": seoyun, "name": "한서윤", "currentAge": 24, "currentLevel": None,
             "profileJson": {"직업": {"value": "검사"}}, "statsJson": None, "skillsJson": None,
             "itemsJson": {"의수": envelope}, "statusesJson": {"왼팔": {"value": "상실"}, "time.현재": {"value": "겨울"}},
             "currentNarrativeTime": None, "manuallyEditedSlots": [], "snapshotVersion": 3, "createdAt": "2026-10-01"},
            {"id": dogyeom, "name": "도겸", "currentAge": None, "currentLevel": None, "profileJson": {"직업": "마법사"},
             "statsJson": None, "skillsJson": {"불 마법": {"value": "사용하면 한참 기침을 한다"}}, "itemsJson": None,
             "statusesJson": None, "currentNarrativeTime": None, "manuallyEditedSlots": [], "snapshotVersion": 2,
             "createdAt": "2026-09-30"},
            {"id": bartan, "name": "바르탄", "currentAge": 51, "currentLevel": None, "profileJson": {"직업": {"value": "대장장이"}},
             "statsJson": None, "skillsJson": None, "itemsJson": None, "statusesJson": None, "currentNarrativeTime": None,
             "manuallyEditedSlots": [{"factType": "AGE", "factKey": "age"}], "snapshotVersion": 1,
             "createdAt": "2026-09-29"},
        ],
        "snapshotSources": [{"characterId": dogyeom, "factType": "PROFILE", "factKey": "직업", "sourceOrder": 1,
                             "factValue": "마법사", "sourceEpisodeNo": 1}],
        "discoveries": [{"id": "00000000-0000-4000-8000-0000000d0001", "matchedCharacterId": seoyun,
                         "entityName": "서윤", "createdAt": "2026-10-01", "sourceEpisodeNo": 1}],
        "worldSettings": [
            {"id": "00000000-0000-4000-8000-0000000e0001", "category": "PLACE", "subjectName": "회색탑",
             "normalizedSubjectName": "회색탑", "version": 2,
             "propertiesJson": {"규칙": "탑 안에서는 어떤 마법도 일어나지 않는다", "문": "해가 진 뒤에는 열리지 않는다"},
             "manuallyEditedPaths": [], "latestSourceEpisodeNo": 2, "earliestSourceEpisodeNo": 1},
            {"id": "00000000-0000-4000-8000-0000000e0002", "category": "PLACE", "subjectName": "은빛 강",
             "normalizedSubjectName": "은빛강", "version": 1, "propertiesJson": {"금기": "밤에 건너면 돌아오지 못한다"},
             "manuallyEditedPaths": ['[null,"금기"]'], "latestSourceEpisodeNo": 1, "earliestSourceEpisodeNo": 1},
        ],
        "worldHistory": [
            {"candidateId": "00000000-0000-4000-8000-0000000f0001",
             "targetWorldSettingId": "00000000-0000-4000-8000-0000000e0001", "category": "PLACE",
             "subjectName": "회색탑", "scopeName": None, "settingName": "문", "value": "해가 진 뒤에는 열리지 않는다",
             "operation": "ADD", "historyOnly": False, "effectiveFromEpisodeNo": 2, "temporalScope": "PRESENT"},
        ],
    }


def write(directory):
    """Write `manuscripts/NNN화 *.txt` and `baseline-raw.json` under `directory`; return their paths."""
    directory = Path(directory)
    manuscripts = directory / "manuscripts"
    manuscripts.mkdir(parents=True, exist_ok=True)
    texts = {}
    for no, (name, text) in EPISODES.items():
        (manuscripts / name).write_bytes(text.encode("utf-8"))
        texts[no] = text
    raw = write_json(directory / "baseline-raw.json", raw_export(texts))
    return manuscripts, raw
