"""Model flattening and identifier hashing."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from linkedin_marketing import hash_email
from linkedin_marketing.models import AdAccount, AdLibraryAd, CampaignGroup, Money


def test_hash_email_normalises_and_passes_digests_through():
    expected = hashlib.sha256(b"someone@example.com").hexdigest()
    assert hash_email("  Some One@Example.COM ".replace(" One", "one")) == expected
    assert hash_email(expected.upper()) == expected
    with pytest.raises(ValueError):
        hash_email("not-an-email")


def test_money_is_2dp_string():
    assert Money(Decimal("70.000"), "GBP").to_dict() == {"amount": "70.00", "currency": "GBP"}
    assert Money.from_api({"amount": "x"}) is None


def test_to_dict_drops_raw_and_is_json_safe():
    group = CampaignGroup.from_api(
        {
            "id": 1,
            "name": "G",
            "status": "ACTIVE",
            "totalBudget": {"amount": "500", "currencyCode": "EUR"},
            "runSchedule": {"start": 0},
        }
    )
    record = group.to_dict()
    assert "raw" not in record
    assert record["total_budget"] == {"amount": "500.00", "currency": "EUR"}
    assert AdAccount.from_api({"id": "5"}).id == 5


def test_ad_library_ad_tolerates_missing_details():
    ad = AdLibraryAd.from_api({"adUrl": "u"})
    assert ad.advertiser_name is None
    assert ad.impressions is None
