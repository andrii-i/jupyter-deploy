# mypy: disable-error-code=name-defined
c = get_config()  # noqa

c.Application.log_level = "INFO"
c.ServerApp.root_dir = "/home/jovyan"
