import argparse
import itertools
import os
import cherrypy
import platform
import signal
import twisted.internet
import sys
import time
import ws4py.server
import json

import tangelo
from   tangelo.minify_json import json_minify
import tangelo.server
import tangelo.tool
import tangelo.util
import tangelo.websocket

def read_config(cfgfile):
    if cfgfile is None:
        return {}

    # Read out the text of the file.
    with open(cfgfile) as f:
        text = f.read()

    # Strip comments and then parse into a dict.
    return json.loads(json_minify(text))

def polite(signum, frame):
    print >>sys.stderr, "Already shutting down.  To force shutdown immediately, send SIGQUIT (Ctrl-\\)."

def die(signum, frame):
    print >>sys.stderr, "Forced shutdown.  Exiting immediately."
    os.kill(os.getpid(), signal.SIGKILL)

def shutdown(signum, frame):
    # Disbale the shutdown handler (i.e., for repeated Ctrl-C etc.) for the
    # "polite" shutdown signals.
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, polite)

    # Perform (1) vtkweb process cleanup, (2) twisted reactor cleanup and quit,
    # (3) CherryPy shutdown, and (4) CherryPy exit.
    tangelo.server.cpserver.root.cleanup()
    twisted.internet.reactor.stop()
    cherrypy.engine.stop()
    cherrypy.engine.exit()

def start():
    sys.stderr.write("starting tangelo...")

    # The presence of a pid file means that either this instance of Tangelo is
    # already running, or the PID is stale.
    if os.path.exists(pidfile):
        # Get the pid.
        try:
            pid = tangelo.util.read_pid(pidfile)
        except ValueError:
            sys.stderr.write("failed (pidfile exists and contains bad pid)\n")
            return 1

        # Check if the pid is live - if so, then Tangelo is already running; if
        # not, then remove the pidfile.
        if tangelo.util.live_pid(pid):
            sys.stderr.write("failed (already running)\n")
            return 1
        else:
            try:
                os.remove(pidfile)
            except OSError:
                sys.stderr.write("failed (could not remove stale pidfile)")
                return 1

    # Make sure the working directory is the place where the control script
    # lives.
    #os.chdir(path)

    # Set up the global configuration.  This includes the hostname and port
    # number as specified in the CMake phase.
    #
    # Whether to log directly to the screen has to do with whether we are
    # daemonizing - if we are, we want to suppress the output, and if we are
    # not, we want to see everything.
    cherrypy.config.update({"environment": "production",
                            "log.error_file": logfile,
                            "log.screen": not daemonize,
                            "server.socket_host": hostname,
                            "server.socket_port": port,
                            "error_page.default": tangelo.server.Tangelo.error_page})

    # If we are daemonizing, do it here, before anything gets started.  We have
    # to set this up in a certain way:
    #
    # 1. We fork ourselves immediately, so the child process, which will
    # actually start CherryPy, doesn't scribble on the screen.
    #
    # 2. We get the parent process to poll the logfile for specific messages
    # indicating success or failure, and use these to print an informative
    # message on screen.
    #
    # The special behavior of the parent before it exits is the reason we don't
    # just use the CherryPy Daemonizer plugin.
    if daemonize:
        fork = os.fork()

        # The parent process - start a polling loop to watch for signals in the
        # log file before exiting.
        if fork != 0:
            # Loop until we can open the logfile (this is in case the child
            # process hasn't created it just yet).
            opened = False
            while not opened:
                try:
                    f = open(logfile)
                    opened = True
                except IOError:
                    pass

            # Seek to the end of the file.
            f.seek(0, os.SEEK_END)

            # In a loop, look for new lines being added to the log file, and
            # examine them for signs of success or failure.
            done = False
            location = None
            while not done:
                cur_pos = f.tell()
                line = f.readline()
                if not line:
                    f.seek(cur_pos)
                else:
                    if "Bus STARTED" in line:
                        retval = 0
                        print >>sys.stderr, "success (serving on %s)" % (location)
                        done = True
                    elif "Error" in line:
                        retval = 1
                        print >>sys.stderr, "failed (check tangelo.log for reason)"
                        done = True
                    elif "Serving on" in line:
                        location = line.split("Serving on")[1].strip()

            # The parent process can now exit, indicating success or failure of
            # the child.
            sys.exit(retval)

    # From this point forward, we are the child process, and can now set up the
    # server and get it going.
    #
    # Create an instance of the main handler object.
    tangelo.server.cpserver = cherrypy.Application(tangelo.server.Tangelo(vtkweb_port_list), "/")
    cherrypy.tree.mount(tangelo.server.cpserver, config={"/": { "tools.auth_update.on": access_auth,
                                                                "tools.treat_url.on": True }})

    # Try to drop privileges if requested, since we've bound to whatever port
    # superuser privileges were needed for already.
    if drop_privileges:
        # If we're on windows, don't supply any username/groupname, and just
        # assume we should drop priveleges.
        if os_name == "Windows":
            cherrypy.process.plugins.DropPrivileges(cherrypy.engine).subscribe()
        elif os.getuid() == 0:
            # Reaching here means we're on unix, and we are the root user, so go
            # ahead and drop privileges to the requested user/group.
            import grp
            import pwd

            # Find the UID and GID for the requested user and group.
            try:
                mode = "user"
                value = user
                uid = pwd.getpwnam(user).pw_uid

                mode = "group"
                value = group
                gid = grp.getgrnam(group).gr_gid
            except KeyError:
                msg = "no such %s '%s' to drop privileges to" % (mode, value)
                tangelo.log(msg, "ERROR")
                print >>sys.stderr, "failed (%s)" % (msg)
                sys.exit(1)

            # Set the process home directory to be the dropped-down user's.
            os.environ["HOME"] = os.path.expanduser("~%s" % (user))

            # Transfer ownership of the log file to the non-root user.
            os.chown(logfile, uid, gid)

            # Perform the actual UID/GID change.
            cherrypy.process.plugins.DropPrivileges(cherrypy.engine, uid=uid, gid=gid).subscribe()

    # If daemonizing, we need to maintain a pid file.
    if daemonize:
        cherrypy.process.plugins.PIDFile(cherrypy.engine, pidfile).subscribe()

    # Set up websocket handling.  Use the pass-through subclassed version of the
    # plugin so we can set a priority on it that doesn't conflict with privilege
    # drop.
    tangelo.websocket.WebSocketLowPriorityPlugin(cherrypy.engine).subscribe()
    cherrypy.tools.websocket = ws4py.server.cherrypyserver.WebSocketTool()

    # Replace the stock auth_digest and auth_basic tools with ones that have
    # slightly lower priority (so the AuthUpdate tool can run before them).
    cherrypy.tools.auth_basic = cherrypy.Tool("before_handler", cherrypy.lib.auth_basic.basic_auth, priority=2)
    cherrypy.tools.auth_digest = cherrypy.Tool("before_handler", cherrypy.lib.auth_digest.digest_auth, priority=2)

    # Install signal handlers to allow for proper cleanup/shutdown.
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, shutdown)

    # Send SIGQUIT to an immediate, ungraceful shutdown instead.
    signal.signal(signal.SIGQUIT, die)

    # Install the "treat_url" tool, which performs redirections and analyzes the
    # request path to see what kind of resource is being requested, and the
    # "auth update" tool, which checks for updated/new/deleted .htaccess files
    # and updates the state of auth tools on various paths.
    cherrypy.tools.treat_url = cherrypy.Tool("before_handler", tangelo.tool.treat_url, priority=0)
    if access_auth:
        cherrypy.tools.auth_update = tangelo.tool.AuthUpdate(point="before_handler", priority=1)

    # Start the CherryPy engine.
    cherrypy.engine.start()

    # Start the Twisted reactor in the main thread (it will block but the
    # CherryPy engine has already started in a non-blocking manner).
    twisted.internet.reactor.run(installSignalHandlers=False)
    cherrypy.engine.block()

def stop():
    if pidfile is None:
        raise TypeError("'pidfile' argument is required")

    retval = 0
    sys.stderr.write("stopping tangelo...")

    if os.path.exists(pidfile):
        # Read the pid.
        try:
            pid = tangelo.util.read_pid(pidfile)
        except ValueError:
            sys.stderr.write("failed (tangelo.pid does not contain a valid process id)\n")
            return 1

        # Attempt to terminate the process, if it's still alive.
        try:
            if tangelo.util.live_pid(pid):
                os.kill(pid, signal.SIGTERM)
                while tangelo.util.live_pid(pid):
                    time.sleep(0.1)
        except OSError:
            sys.stderr.write("failed (could not terminate process %d)\n" % (pid))
            retval = 1

    if retval == 0:
        sys.stderr.write("success\n")

    return retval

def restart():
    stopval = stop()
    if stopval == 0:
        return start()
    else:
        return stopval
if __name__ == "__main__":
    # Twiddle the command line so the name of the program is "tangelo" rather
    # than "__main__".
    sys.argv[0] = sys.argv[0].replace("__main__.py", "tangelo")

    p = argparse.ArgumentParser(description="Control execution of a Tangelo server.")
    p.add_argument("-c", "--config", type=str, default=None, metavar="FILE", help="specifies configuration file to use")
    p.add_argument("-d", "--daemonize", action="store_const", const=True, default=None, help="run Tangelo as a daemon (default)")
    p.add_argument("-nd", "--no-daemonize", action="store_const", const=True, default=None, help="run Tangelo in-console (not as a daemon)")
    p.add_argument("-a", "--access-auth", action="store_const", const=True, default=None, help="enable HTTP authentication (i.e. processing of .htaccess files) (default)")
    p.add_argument("-na", "--no-access-auth", action="store_const", const=True, default=None, help="disable HTTP authentication (i.e. processing of .htaccess files)")
    p.add_argument("-p", "--drop-privileges", action="store_const", const=True, default=None, help="enable privilege drop when started as superuser (default)")
    p.add_argument("-np", "--no-drop-privileges", action="store_const", const=True, default=None, help="disable privilege drop when started as superuser")
    p.add_argument("--hostname", type=str, default=None, metavar="HOSTNAME", help="overrides configured hostname on which to run Tangelo")
    p.add_argument("--port", type=int, default=None, metavar="PORT", help="overrides configured port number on which to run Tangelo")
    p.add_argument("--vtkweb-ports", type=str, default=None, metavar="PORT RANGE", help="specifies the port range to use for VTK Web processes")
    p.add_argument("-u", "--user", type=str, default=None, metavar="USERNAME", help="specifies the user to run as when root privileges are dropped")
    p.add_argument("-g", "--group", type=str, default=None, metavar="GROUPNAME", help="specifies the group to run as when root privileges are dropped")
    p.add_argument("--logdir", type=str, default=None, metavar="DIR", help="where to place the log file (rather than in the directory where this program is")
    p.add_argument("--piddir", type=str, default=None, metavar="DIR", help="where to place the PID file (rather than in the directory where this program is")
    p.add_argument("action", metavar="<start|stop|restart>", help="perform this action for the current Tangelo instance.")
    args = p.parse_args()

    # Make sure user didn't specify conflicting flags.
    if args.daemonize and args.no_daemonize:
        print >>sys.stderr, "error: can't specify both --daemonize (-d) and --no-daemonize (-nd) together"
        sys.exit(1)

    if args.access_auth and args.no_access_auth:
        print >>sys.stderr, "error: can't specify both --access-auth (-a) and --no-access-auth (-na) together"
        sys.exit(1)

    if args.drop_privileges and args.no_drop_privileges:
        print >>sys.stderr, "error: can't specify both --drop-privileges (-p) and --no-drop-privileges (-np) together"
        sys.exit(1)

    # Before extracting the other arguments, find a configuration file.  Check
    # the command line arguments first, then look for one in a sequence of other
    # places.
    cfg_file = args.config
    if cfg_file is None:
        for loc in ["/etc/tangelo.conf", os.path.expanduser("~/.config/tangelo/tangelo.conf")]:
            if os.path.exists(loc):
                cfg_file = loc
                break

    # Get a dict representing the contents of the config file.
    config = read_config(cfg_file)

    # Decide whether to daemonize, based on whether the user wishes not to, and
    # whether the platform supports it.
    #
    # First detect the operating system (and OSX version, if applicable).
    os_name = platform.system()
    if os_name == "Darwin":
        version = map(int, platform.mac_ver()[0].split("."))

    # Determine whether to daemonize.
    daemonize_flag = True
    if args.daemonize is None and args.no_daemonize is None:
        if config.get("daemonize") is not None:
            daemonize_flag = config.get("daemonize")
    else:
        daemonize_flag = (args.daemonize is not None) or (not args.no_daemonize)
    daemonize = daemonize_flag and not(os_name == "Windows" or (os_name == "Darwin" and version[1] == 6))

    # Determine whether to use access auth.
    access_auth = True
    if args.access_auth is None and args.no_access_auth is None:
        if config.get("access_auth") is not None:
            access_auth = config.get("access_auth")
    else:
        access_auth = (args.access_auth is not None) or (not args.no_access_auth)

    # Determine whether to perform privilege drop.
    drop_privileges = True
    if args.drop_privileges is None and args.no_drop_privileges is None:
        if config.get("drop_privileges") is not None:
            drop_privileges = config.get("drop_privileges")
    else:
        drop_privileges = (args.drop_privileges is not None) or (not args.no_drop_privileges)

    # Extract the rest of the arguments, giving priority first to command line
    # arguments, then to the configuration file (if any), and finally to a
    # hard-coded default value.
    action = args.action
    hostname = args.hostname or config.get("hostname") or "localhost"
    port = args.port or config.get("port") or 8080
    user = args.user or config.get("user") or "nobody"
    group = args.group or config.get("group") or "nobody"
    logdir = os.path.abspath(args.logdir or config.get("logdir") or ".")
    piddir = os.path.abspath(args.piddir or config.get("piddir") or ".")

    vtkweb_port_list = []
    if args.vtkweb_ports is not None:
        try:
            # This parses expressions of the form "8081-8090,12000,13500-13600".
            # It will allow spaces around the punctuation, but reject too many
            # hyphens, and "blank" entries (i.e. two commas next to each other
            # etc.).
            vtkweb_port_list = list(itertools.chain.from_iterable(map(lambda y: range(y[0], y[1]) if len(y) == 2 else [y[0]], map(lambda x: map(int, x.split("-", 1)), args.vtkweb_ports.split(",")))))
        except ValueError:
            print >>sys.stderr, "error: could not parse VTK Web port range specification '%s'" % (args.vtkweb_ports)
            sys.exit(1)

        if vtkweb_port_list == []:
            print >>sys.stderr, "error: VTK Web port specification '%s' produces no ports" % (args.vtkweb_ports)
            sys.exit(1)

        if port in vtkweb_port_list:
            print >>sys.stderr, "error: Tangelo server port %d cannot be part of VTK Web port specification ('%s')" % (port, args.vtkweb_ports)
            sys.exit(1)

    # Determine the current directory based on the invocation of this script.
    current_dir = os.getcwd()
    cherrypy.config.update({"webroot": current_dir + "/web"})

    # Place an empty dict to hold per-module configuration into the global
    # configuration object.
    cherrypy.config.update({"module-config": {}})

    # Name the PID file.
    pidfile = piddir + "/tangelo.pid"

    # Name the log file.
    logfile = logdir + "/tangelo.log"

    # Dispatch on action argument.
    code = 1
    if action == "start":
        code = start()
    elif action == "stop":
        if not daemonize:
            sys.stderr.write("error: stop action not supported on this platform\n")
            sys.exit(1)
        code = stop()
    elif action == "restart":
        if not daemonize:
            sys.stderr.write("error: restart action not supported on this platform\n")
            sys.exit(1)
        code = restart()
    else:
        p.print_usage()
        code = 1

    sys.exit(code)
