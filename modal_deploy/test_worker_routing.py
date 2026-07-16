"""Contract tests for the parallel Modal RVC web endpoints."""

from modal_deploy import worker


def test_legacy_endpoint_keeps_existing_singapore_contract() -> None:
    location = worker.web_function_location("legacy")

    assert location == {
        "region": "ap-southeast",
        "routing_region": None,
        "edge_name": "legacy-us-east-route",
    }


def test_ap_endpoint_uses_broad_capacity_and_mumbai_routing() -> None:
    location = worker.web_function_location("ap")

    assert location == {
        "region": "ap",
        "routing_region": "ap-south",
        "edge_name": "ap-south-route",
    }


def test_both_endpoint_functions_are_deployed_symbols() -> None:
    assert worker.fastapi_app is not None
    assert worker.fastapi_app_ap is not None
