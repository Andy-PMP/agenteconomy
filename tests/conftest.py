"""No test may ever reach a real model. This makes it impossible rather than merely unlikely."""

import boto3
import pytest


def _refuse(*args, **kwargs):
    raise AssertionError("a test tried to create a real AWS client, which could spend money")


@pytest.fixture(autouse=True)
def no_real_aws_clients(monkeypatch):
    monkeypatch.setattr(boto3, "client", _refuse)
    monkeypatch.setattr(boto3, "resource", _refuse)
    monkeypatch.setattr(boto3.session.Session, "client", _refuse)
