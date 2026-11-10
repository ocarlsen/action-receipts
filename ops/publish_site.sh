#!/bin/zsh
# Publish out/site (built by `python -m receipts.site ... --domain x.y`) to a public deploy-only
# repo and turn on GitHub Pages. First run creates the repo; later runs replace its content with
# one new commit. Run it yourself: it makes a public surface.
# Required env: SITE_REPO=owner/repo  SITE_DOMAIN=x.y
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SITE_REPO:?set SITE_REPO=owner/repo}"
: "${SITE_DOMAIN:?set SITE_DOMAIN=x.y}"
REPO=$SITE_REPO
SITE=out/site
[[ -f $SITE/index.html && -f $SITE/CNAME ]] || { echo "no built site with a CNAME in $SITE"; exit 1; }
.venv/bin/python -c "
import sys; from pathlib import Path; from receipts.site import leak_scan
f = leak_scan(Path('$SITE')); print('\n'.join(f) or 'leak scan clean'); sys.exit(1 if f else 0)"
export GH_TOKEN=$(gh auth token --user ocarlsen)
if ! gh repo view $REPO >/dev/null 2>&1; then
  gh repo create $REPO --public \
    --description "Static demo: a member-service agent for a fictional credit union where every action has a receipt"
  NEW=1
fi
WORK=$(mktemp -d)
cp -R $SITE/. $WORK/
cd $WORK
git init -q -b main
git add -A
git -c user.name="Owen Carlsen" -c user.email="ocarlsen@gmail.com" commit -q -m "Publish static demo $(date +%F)"
git -c credential.helper= -c "credential.helper=!f(){ echo username=x-access-token; echo password=$GH_TOKEN; }; f" \
  push -q --force https://github.com/$REPO.git main
if [[ -n ${NEW:-} ]]; then
  gh api -X POST repos/$REPO/pages -f "source[branch]=main" -f "source[path]=/" --jq '.html_url'
fi
gh api repos/$REPO/pages --jq '{url: .html_url, cname, status}'
echo "DNS: CNAME $SITE_DOMAIN -> ocarlsen.github.io. Then enforce HTTPS in the repo's Pages settings."
