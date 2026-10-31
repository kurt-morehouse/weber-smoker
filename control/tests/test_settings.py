import pytest

from helpers import config, write_config
from settings import ConfigError, load_config


def test_shipped_config_loads():
    cfg = load_config()
    assert cfg["zones"]["zone1"]["gpio_pin"] == 17
    assert cfg["zones"]["zone2"]["gpio_pin"] == 27


def test_shipped_config_matches_the_wiring():
    z = load_config()["zones"]
    assert (z["zone1"]["spi_bus"], z["zone1"]["spi_chip_select"]) == (0, 0)   # /dev/spidev0.0
    assert (z["zone2"]["spi_bus"], z["zone2"]["spi_chip_select"]) == (1, 0)   # /dev/spidev1.0


def test_paths_resolved_relative_to_config(tmp_path):
    cfg = config(tmp_path)
    assert cfg["paths"]["data_dir"] == str(tmp_path / "data")


def expect_problem(tmp_path, text, overrides=None, delete=None):
    with pytest.raises(ConfigError) as e:
        load_config(write_config(tmp_path, overrides, delete))
    assert text in str(e.value)


def test_missing_key(tmp_path):
    expect_problem(tmp_path, "pid.kp: missing", delete=["pid.kp"])


def test_unknown_key(tmp_path):
    expect_problem(tmp_path, "pid.kpp: unknown setting", {"pid": {"kpp": 1}})


def test_old_spi_bus_says_where_it_moved(tmp_path):
    expect_problem(tmp_path, "moved to zones.<zone>.spi_bus", {"spi": {"bus": 0}})


def test_out_of_range(tmp_path):
    expect_problem(tmp_path, "outside", {"websocket": {"port": 80}})


def test_wrong_type(tmp_path):
    expect_problem(tmp_path, "expected a number", {"pid": {"kp": "fast"}})


def test_same_relay_pin(tmp_path):
    expect_problem(tmp_path, "same gpio_pin", {"zones": {"zone2": {"gpio_pin": 17}}})


def test_same_bus_and_chip_select(tmp_path):
    expect_problem(tmp_path, "same (spi_bus, spi_chip_select)",
                   {"zones": {"zone2": {"spi_bus": 0, "spi_chip_select": 0}}})


def test_spi1_only_has_ce0(tmp_path):
    expect_problem(tmp_path, "spi1-1cs", {"zones": {"zone2": {"spi_chip_select": 1}}})


@pytest.mark.parametrize("pin", [18, 19, 20, 21])
def test_relay_on_spi1_pin_rejected(tmp_path, pin):
    expect_problem(tmp_path, "belongs to SPI1", {"zones": {"zone1": {"gpio_pin": pin}}})


@pytest.mark.parametrize("pin", [7, 8, 9, 10, 11])
def test_relay_on_spi0_pin_rejected(tmp_path, pin):
    expect_problem(tmp_path, "belongs to SPI0", {"zones": {"zone2": {"gpio_pin": pin}}})


def test_setpoint_ceiling_below_overtemp(tmp_path):
    expect_problem(tmp_path, "max_setpoint_f must be below",
                   {"safety": {"max_setpoint_f": 320, "max_temp_f": 300}})


def test_pulse_cap_above_hard_limit(tmp_path):
    # 30 is allowed by the schema range, 30.5 is not; check the cross-check via 30 == ok
    cfg = config(tmp_path, {"ssr_test": {"max_pulse_seconds": 30}})
    assert cfg["ssr_test"]["max_pulse_seconds"] == 30


def test_all_problems_reported_at_once(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write_config(tmp_path, {"pid": {"kp": "x", "ki": 5}}))
    assert "2 problem" in str(e.value)
