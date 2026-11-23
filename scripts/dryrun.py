# -*- coding: utf-8 -*-
"""在本機乾跑與 GitHub Actions 相同的步驟（不 commit、不推送、不部署；預設也不連網）。

  python -I scripts/dryrun.py validate                     # = validate-on-push.yml
  python -I scripts/dryrun.py daily [--fetch]              # = daily-speedcams.yml（--fetch 才真的下載）
  python -I scripts/dryrun.py weekly --pbf <pbf> [--fetch] # = weekly-osm.yml（需要 pyosmium；沒有 --pbf 就用 --no-osm）
  python -I scripts/dryrun.py monthly [--fetch]            # = monthly-timing.yml
建置一律加 --dry-run（只寫 build/，不動 data/、manifest.json）。真的要更新本機 data/ 時直接跑 scripts/publish.py。
"""
import argparse
import subprocess
import sys

STEPS = {
    'validate': [['-m', 'unittest', 'discover', '-s', 'scripts/tests', '-t', 'scripts'], ['scripts/validate.py'],
                 ['scripts/publish.py', 'speedcams', '--dry-run']],
    'daily': [['-m', 'unittest', 'discover', '-s', 'scripts/tests', '-t', 'scripts'], 'FETCH speedcams-core',
              ['scripts/publish.py', 'speedcams', '--max-drop', '0.05', '--dry-run']],
    'weekly': [['-m', 'unittest', 'discover', '-s', 'scripts/tests', '-t', 'scripts'], 'FETCH signals speedcams-counties', 'WEEKLY'],
    'monthly': [['-m', 'unittest', 'discover', '-s', 'scripts/tests', '-t', 'scripts'], 'FETCH timing speedlimits',
                ['scripts/publish.py', 'timing', 'speedlimits', '--max-drop', '0.05', '--dry-run']],
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('job', choices=sorted(STEPS))
    ap.add_argument('--fetch', action='store_true', help='真的連網下載（每個請求間隔 ≥ 40 秒）')
    ap.add_argument('--pbf')
    a = ap.parse_args(argv)
    for step in STEPS[a.job]:
        if isinstance(step, str) and step.startswith('FETCH'):
            groups = step.split()[1:]
            cmd = ['scripts/fetch.py'] + groups + ([] if a.fetch else ['--dry-run'])
        elif step == 'WEEKLY':
            cmd = ['scripts/publish.py', 'signals'] + (['reststops', '--pbf', a.pbf] if a.pbf else ['--no-osm']) + \
                  ['--max-drop', '0.05', '--dry-run']
        else:
            cmd = step
        full = [sys.executable, '-I'] + cmd
        print('\n== ' + ' '.join(cmd))
        r = subprocess.run(full)
        if r.returncode != 0:
            print('步驟失敗（結束碼 %d），停止' % r.returncode)
            return r.returncode
    print('\n乾跑完成：%s' % a.job)
    return 0


if __name__ == '__main__':
    sys.exit(main())
