"""Small deterministic regression fixture, not an estimate of real-world quality."""
from backend.app.integrated_memory import _map_source


def test_supported_shared_facts_and_unsafe_inputs_are_distinguished():
    positives = [
        'BC250 서버 운영체제는 Fedora입니다.',
        'BC-250의 OS: Fedora 43',
        'BC 250 서버 백업 경로: /srv/backup',
        'BC250 서버 백업 주기는 매일',
        'BC250 client 서비스 포트 = 8010',
    ]
    negatives = [
        'BC250 서버 운영체제는 뭐야?',
        'BC250 서버 운영체제는 Fedora가 아니야',
        'BC250 서버 운영체제는 Fedora일 수도 있어',
        'BC250 서버 운영체제는 Fedora로 바꿀 계획이야',
        '내 서버 BC250 운영체제는 Fedora',
        'BC250 client 포트는 0',
        'BC250 client 포트는 8010 또는 8020',
    ]
    true_positives = sum(_map_source(text)[0] is not None for text in positives)
    false_positives = sum(_map_source(text)[0] is not None for text in negatives)
    assert true_positives == len(positives)
    assert false_positives == 0
    print(f'fixture: supported={true_positives}/{len(positives)} unsafe_selected={false_positives}/{len(negatives)}')
