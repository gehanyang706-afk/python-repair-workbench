import pytest
from itsdangerous import TimestampSigner, SignatureExpired

class ClockSigner(TimestampSigner):
    current = 200
    def get_timestamp(self):
        return self.current

def test_future_timestamp_is_rejected_with_max_age():
    signer = ClockSigner("public-fixture-key")
    signed = signer.sign(b"payload")
    signer.current = 190
    with pytest.raises(SignatureExpired):
        signer.unsign(signed, max_age=20)
