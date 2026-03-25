#!/bin/bash
# Double-click this file to push code and update the server
cd "$(dirname "$0")"
echo "Pushing to GitHub..."
git push origin master 2>&1
echo ""
echo "Updating server..."
curl -s -X POST http://137.184.104.80/api/deploy | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message','Done'))"
echo ""
echo "All done! You can close this window."
read -p ""
