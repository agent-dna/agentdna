"""Setup, once: a £40 test payment in Stripe, for the example to refund.

    python make_payment.py

Prints the payment's id: put it in .env as STRIPE_PAYMENT_INTENT. This setup
script calls Stripe directly with your test key; the agents never do.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

response = httpx.post(
    "https://api.stripe.com/v1/payment_intents",
    headers={"Authorization": os.environ["STRIPE_AUTH"]},
    data={
        "amount": 4000,  # in pence: £40
        "currency": "gbp",
        "payment_method": "pm_card_visa",  # Stripe's test card
        "confirm": "true",
        "automatic_payment_methods[enabled]": "true",
        "automatic_payment_methods[allow_redirects]": "never",  # confirm without a redirect
    },
)
response.raise_for_status()
print(f"STRIPE_PAYMENT_INTENT={response.json()['id']}")
