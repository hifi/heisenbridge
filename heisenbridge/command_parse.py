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


class CommandManager:
    _commands: dict

    def __init__(self):
        self._commands = {}

    def register(self, cmd: CommandParser, func):
        self._commands[cmd.prog] = (cmd, func)

    async def trigger(self, text):
        args = shlex.split(text)
        command = args.pop(0).upper()

        if command in self._commands:
            (cmd, func) = self._commands[command]
            return await func(cmd.parse_args(args))
        elif command == "HELP":
            out = ["Following commands are supported:", ""]
            for (cmd, func) in self._commands.values():
                out.append("\t{} - {}".format(cmd.prog, cmd.short_description))

            out.append("")
            out.append("To get more help, add -h to any command without arguments.")

            raise CommandParserError("\n".join(out))
        else:
            raise CommandParserError('Unknown command "{}", type HELP for list'.format(command))
