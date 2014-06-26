import json
import traceback

import cherrypy

import tangelo
import tangelo.tool


class TangeloStream(object):
    exposed = True

    def __init__(self, module_cache=None):
        self.modules = tangelo.util.ModuleCache() if module_cache is None else module_cache
        self.streams = {}

    def GET(self, key=None):
        if key is None:
            result = self.streams.keys()
        elif key not in self.streams:
            cherrypy.response.status = "400 No Such Key"
            result = {"error": "Key '%s' does not correspond to an active stream" % (key)}
        else:
            # Grab the stream in preparation for running it.
            stream = self.streams[key]

            # Attempt to run the stream via its next() method - if this
            # yields a result, then continue; if the next() method raises
            # StopIteration, then there are no more results to retrieve; if
            # any other exception is raised, this is treated as an error.
            try:
                result = {"data": stream.next()}
            except StopIteration:
                result = {"finished": True}
                del self.streams[key]
            except:
                del self.streams[key]
                raise cherrypy.HTTPError(
                    "501 Error in Python Service",
                    "Caught exception while executing stream " +
                    "service keyed by %s:<br><pre>%s</pre>" %
                    (key, traceback.format_exc()))

        try:
            return json.dumps(result)
        except TypeError:
            raise cherrypy.HTTPError(
                "501 Bad Response from Python Service",
                ("The stream keyed by %s returned a non " +
                 "JSON-seriazable result: %s") % (key, result["data"]))

    def POST(self, *pathcomp, **kwargs):
        url = "/" + "/".join(pathcomp)
        directive = tangelo.tool.analyze_url(url, cherrypy.config.get("webroot"))

        if "target" not in directive or directive["target"].get("type") != "service":
            tangelo.log(json.dumps(directive, indent=4))
            cherrypy.response.status = "500 Error Opening Streaming Service"
            result = {"error": "could not open streaming service"}
        else:
            # Extract the path to the service and the list of positional
            # arguments.
            module_path = directive["target"]["path"]
            pargs = directive["target"]["pargs"]

            # Get the service module.
            try:
                service = self.modules.get(module_path)
            except tangelo.HTTPStatusCode as e:
                if e.msg:
                    raise cherrypy.HTTPError(e.code, e.msg)
                else:
                    raise cherrypy.HTTPError(e.code)

            # Check for a "stream" function inside the module.
            if "stream" not in dir(service):
                cherrypy.response.status = "400 Non-Streaming Service"
                result = {"error": "The requested streaming service does not implement a 'stream()' function"}
            else:
                # Call the stream function and capture its result.
                try:
                    stream = service.stream(*pargs, **kwargs)
                except Exception as e:
                    bt = traceback.format_exc()

                    tangelo.log("Caught exception while executing service %s" %
                                (tangelo.request_path()), "SERVICE")
                    tangelo.log(bt, "SERVICE")

                    cherrypy.response.status = "500 Streaming Service Raised Exception"
                    result = {"error": "Caught exception during streaming service execution: %s" % (str(bt))}
                else:
                    result = self.add(stream)

        return json.dumps(result)

    def DELETE(self, key=None):
        if key is None:
            cherrypy.response.status = "400 No Key Specified"
            result = {"error": "A key is required for this operation, but none was specified"}
        elif key not in self.streams:
            cherrypy.response.status = "400 No Such Key"
            result = {"error": "Key '%s' does not correspond to an active stream" % (key)}
        else:
            del self.streams[key]
            result = {"key": key}

        return json.dumps(result)

    def add(self, stream):
        # Generate a key corresponding to this object.
        key = tangelo.util.generate_key(self.streams)

        # Log the object in the streaming table.
        self.streams[key] = stream

        # Create an object describing the logging of the generator object.
        return {"key": key}
