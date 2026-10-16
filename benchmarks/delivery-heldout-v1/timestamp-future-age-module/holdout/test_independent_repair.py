import pytest
from itsdangerous import TimestampSigner, SignatureExpired

class ClockSigner(TimestampSigner):
    current = 500
    def get_timestamp(self):
        return self.current

@pytest.mark.parametrize("difference", [1, 17])
@pytest.mark.parametrize("return_timestamp", [False, True])
def test_future_age_rejects_consistently(difference, return_timestamp):
    signer = ClockSigner("acceptance-fixture-key")
    signed = signer.sign(b"independent")
    signer.current -= difference
    with pytest.raises(SignatureExpired) as failure:
        signer.unsign(signed, max_age=100, return_timestamp=return_timestamp)
    assert failure.value.payload == b"independent"
    assert failure.value.date_signed is not None


def test_normal_boundary_and_unlimited_validation_preserved():
    signer = ClockSigner("acceptance-fixture-key")
    signed = signer.sign(b"independent")
    signer.current = 510
    assert signer.unsign(signed, max_age=10) == b"independent"
    signer.current = 499
    assert signer.unsign(signed) == b"independent"
