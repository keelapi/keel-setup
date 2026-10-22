#!/usr/bin/env python3
"""Credential-free, read-only voice preparation from an immutable public bundle."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def prepare(bundle: Path, bundle_sha: str, profile_id: str) -> dict:
    if not re.fullmatch(r'[a-f0-9]{40}', bundle_sha):
        raise ValueError('An immutable setup release SHA is required')
    revision = subprocess.run(['git','-C',str(bundle),'rev-parse','HEAD'], check=True, text=True, capture_output=True).stdout.strip()
    if revision != bundle_sha:
        raise ValueError('Bundle revision differs from the requested release')
    dirty = subprocess.run(['git','-C',str(bundle),'status','--porcelain','--untracked-files=all'], check=True, text=True, capture_output=True).stdout
    if dirty.strip():
        raise ValueError('The pinned release must have no local modifications')
    subprocess.run([sys.executable,str(bundle/'scripts/check_release_bundle.py')], check=True, capture_output=True)
    raw = (bundle/'keel-setup/reference/integration_profiles.v1.json').read_bytes()
    contract = json.loads(raw)
    if contract.get('version') != 'keel.integration_profiles.v1' or contract.get('qualification_status') != 'unqualified':
        raise ValueError('Unsupported qualification contract')
    profile = next((row for row in contract['profiles'] if row['id'] == profile_id), None)
    if profile is None or profile['readiness'] != 'preparation_only':
        raise ValueError('Requested voice transport is not supported for preparation')
    return {'version':'keel.voice_preparation.v1','stage':'preparation_only','evidence':'source-inspected',
        'bundle_sha':bundle_sha,'contract_digest':hashlib.sha256(raw).hexdigest(), 'profile':profile,
        'unresolved_gates':contract['voice_gates'], 'activation':'human_owned',
        'next_step':'Follow the pinned voice-gateway reference; designate sandbox credentials outside conversation.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--bundle-sha',required=True)
    parser.add_argument('--profile',required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.bundle.resolve(),args.bundle_sha,args.profile)
    except (ValueError,OSError,subprocess.SubprocessError):
        print(json.dumps({'stage':'blocked','reason':'Pinned voice preparation contract could not be verified.'}))
        return 1
    print(json.dumps(result,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
