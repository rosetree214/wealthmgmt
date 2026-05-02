from core.auto_rebalancer import build_parser


def test_cli_defaults_to_dry_run_unless_execute_flag_is_present():
    parser = build_parser()

    default_args = parser.parse_args(["--once"])
    execute_args = parser.parse_args(["--once", "--execute"])

    assert default_args.execute is False
    assert default_args.dry_run is True
    assert execute_args.execute is True
