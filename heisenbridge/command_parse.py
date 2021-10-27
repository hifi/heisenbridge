import argparse
import shlex


class CommandParserFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


class CommandParserError(Exception):
    pass


class CommandParser(argparse.ArgumentParser):
    def __init__(self, *args, formatter_class=CommandParserFormatter, **kwargs):
        super().__init__(*args, formatter_class=formatter_class, **kwargs)

    @property
    def short_description(self):
        return self.description.split("\n")[0]

    def error(self, message):
        raise CommandParserError(message)

    def print_usage(self):
        raise CommandParserError(self.format_usage())

    def print_help(self):
        raise CommandParserError(self.format_help())

    def exit(self, status=0, message=None):
        pass


def split(text):
    commands = []

    sh_split = shlex.shlex(text, posix=True, punctuation_chars=";")
    sh_split.commenters = ""
    sh_split.wordchars += "!#$%&()*+,-./:<=>?@[\\]^_`{|}~"

    args = []
    for v in list(sh_split):
        if v == ";":
            commands.append(args)
            args = []
        else:
            args.append(v)

    if len(args) > 0:
        commands.append(args)

    return commands


class CommandManager:
    _commands: dict

    def __init__(self):
        self._commands = {}

    def register(self, cmd: CommandParser, func, aliases=None):
        self._commands[cmd.prog] = (cmd, func)

        if aliases is not None:
            for alias in aliases:
                self._commands[alias] = (cmd, func)

    async def trigger_args(self, args, tail=None, allowed=None, forward=None):
        command = args.pop(0).upper()

        if allowed is not None and command not in allowed:
            raise CommandParserError(f"Illegal command supplied: '{command}'")

        if command in self._commands:
            (cmd, func) = self._commands[command]
            cmd_args = cmd.parse_args(args)
            cmd_args._tail = tail
            cmd_args._forward = forward
            await func(cmd_args)
        elif command == "HELP":
            out = ["Following commands are supported:", ""]
            for name, (cmd, func) in self._commands.items():
                if cmd.prog == name:
                    out.append("\t{} - {}".format(cmd.prog, cmd.short_description))

            out.append("")
            out.append("To get more help, add -h to any command without arguments.")

            raise CommandParserError("\n".join(out))
        else:
            raise CommandParserError('Unknown command "{}", type HELP for list'.format(command))

    async def trigger(self, text, tail=None, allowed=None, forward=None):
        for args in split(text):
            await self.trigger_args(args, tail, allowed, forward)
            tail = None
