# get_creds.py
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
import os

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=POLYGON,
    key=os.environ["PRIVATE_KEY"],
)

creds = client.create_or_derive_api_creds()
print("API_KEY       =", creds.api_key)
print("API_SECRET    =", creds.api_secret)
print("API_PASSPHRASE=", creds.api_passphrase)
