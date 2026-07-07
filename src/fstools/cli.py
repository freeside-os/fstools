import click
from fstools.common import load_config
from fstools.pkg import pkg_group
from fstools.image import image_group
from fstools.initrd import initrd_group

@click.group()
@click.option('--simple', is_flag=True, help="Produce simplified, script-friendly output.")
@click.pass_context
def main(ctx, simple):
    """Freeside OS Developer and Builder Tools (fstools)"""
    ctx.ensure_object(dict)
    ctx.obj['SIMPLE'] = simple
    ctx.obj['CONFIG'] = load_config()

# Register command groups
main.add_command(pkg_group)
main.add_command(image_group)
main.add_command(initrd_group)

if __name__ == "__main__":
    main(obj={})
