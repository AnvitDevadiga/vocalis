"""Local tenant bootstrap; prints the API key once."""
import argparse
import json
import secrets
from app import hash_key, store

parser = argparse.ArgumentParser()
parser.add_argument('name')
parser.add_argument('--phone-number',default='')
parser.add_argument('--transfer-number',default='')
args = parser.parse_args()
key = secrets.token_urlsafe(32)
tenant_id = store.create_tenant(args.name,hash_key(key),{'phone_number':args.phone_number,'transfer_number':args.transfer_number})
print(json.dumps({'tenant_id':tenant_id,'api_key':key}))
