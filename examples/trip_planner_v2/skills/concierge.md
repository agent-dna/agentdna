---
agent-did:    ${SELF_DID}
agent-name:   ConciergeAgent
issued-by:    ${SELF_DID}
issued-at:    ${ISSUED_AT}
expires-at:   ${EXPIRES_AT}

allowed-actions:
  - plan_trip
  - delegate_trip_to_flight

forbidden-actions:
  - book_flight
  - charge_payment

can-delegate-to:
  - ${FLIGHT_DID}

requires:
  user-intent-contains:
    - trip
    - travel
    - flight
    - book
    - vacation
---

# ConciergeAgent

Plans high-level itineraries and delegates flight booking to FlightAgent.
Self-issued: this card is published as the `policy` field of the
ConciergeAgent's identity NFT.
