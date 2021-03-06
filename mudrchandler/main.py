import asyncio
import json
import logging
import subprocess
from os import environ as ENV
from os import path
from uuid import uuid1

import jsonapi_requests
from aiofiles import open
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError
from aiosparql.client import SPARQLClient
from aiosparql.syntax import (IRI, RDF, Node, RDFTerm, Triples, escape_any,
                              escape_string)

from mudrchandler.stack import Stack
from mudrchandler.uuidstack import UUIDStack
from mudrchandler.uristack import URIStack

logger = logging.getLogger(__name__)


if ENV.get("ENV", "prod").startswith("dev"):
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)


class Application(web.Application):
    sparql_timeout = 60
    run_command_timeout = 60

    @property
    def sparql(self):
        """
        The SPARQL client
        """
        if not hasattr(self, '_sparql'):
            self._sparql = SPARQLClient(ENV['MU_SPARQL_ENDPOINT'],
                                        graph=IRI(ENV['MU_APPLICATION_GRAPH']),
                                        loop=self.loop,
                                        read_timeout=self.sparql_timeout)
        return self._sparql


    async def run_command(self, *args, timeout=None) -> int:
        """
        Run command in subprocess and wait until completion.
        """
        if timeout is None:
            timeout = self.run_command_timeout
            
        # Create subprocess
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE, # accessible as process.stdout
            loop=self.loop)

        # Wait for the subprocess to finish
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            logger.warn(
                "Child process %d awaited for too long, terminating...",
                process.pid)
            try:
                process.terminate()
            except Exception:
                pass
        return process.returncode


    async def handle_fetch_drc(self, request):
        """
        Handle the request to fetch a docker-compose 
        file & save it into the database.

        Arguments:
            request: the request object coming from the 
                     /createdrc POST entrypoint
        
        Notes: 
            The structure of the data available in this
            entrypoint is:
            {
                uuid: uuid of the stack.
                app: instance of the application
            }

            The stacks' URI is not available in this entrypoint,
            logic to retrieve it has to be implemented.
        """
        try:
            data = await request.json()
        except:
            raise web.HTTPBadRequest(body="invalid json")

        data.update({ 'app': self })
        stack = UUIDStack(data)
        docker_compose = await self.docker_compose(stack)
        try:
            drc_uri = await self.create_drc_db(docker_compose, stack)
            await self.update_stack_drc(drc_uri, stack)
            return web.Response(body=json.dumps({
                "data": {
                    "attributes": {
                        "status": 200,
                        "message": "DockerCompose created and Stack updated"
                    },
                    "type": "mu-docker-compose-handler"
                }}))
        except AttributeError:
            raise web.HTTPInternalServerError(body=json.dumps({
                "status": 500,
                "title": "Stack already has DockerCompose",
                "detail": "Stack {} already has DockerCompose linked to it".format(stack.uuid)
            }))

    async def ensure_stack_has_drc(self, uuid: str) -> bool:
        """
        Return True if the Stack has already a docker-compose.yml
        file associated and False otherwise.

        Arguments:
            uri: the URI of the given Stack
        
        Returns: boolean 
        """
        result = await self.sparql.query("""
            ASK FROM {{graph}} WHERE { 
                ?s <http://mu.semte.ch/vocabularies/core/uuid> {{}} .
                ?s <http://swarmui.semte.ch/vocabularies/core/dockerComposeFile> ?x .
            }
            """, escape_string(uuid))
        return result['boolean']


    async def create_drc_db(self, drc: str, stack: Stack) -> str:
        """
        Create the DockerCompose model in the DB.

        Arguments:
            drc: string with the docker-compose file contents
        """
        drc_uuid = uuid1().hex
        stack_uuid = stack.uuid if isinstance(stack, UUIDStack) else await stack.uuid
        uri = "http://stack-builder.big-data-europe.eu/resources/docker-composes/{}".format(drc_uuid)
        await self.sparql.update("""
            PREFIX dct: <http://purl.org/dc/terms/>
            PREFIX mu: <http://mu.semte.ch/vocabularies/core/>
            PREFIX stackbuilder: <http://stackbuilder.semte.ch/vocabularies/core/>
            INSERT DATA 
            {
                GRAPH {{graph}} {
                    <{{uri}}> a stackbuilder:DockerCompose.
                    <{{uri}}> mu:uuid {{drc_uuid}}.
                    <{{uri}}> stackbuilder:text {{drc}}.
                    <{{uri}}> dct:title {{title}}.
                }
            }
            """,
            uri=uri,
            drc_uuid=escape_string(drc_uuid), 
            drc=escape_string(drc), 
            title=escape_string("stack_{}_drc_{}".format(stack_uuid, drc_uuid)))
        return uri


    async def update_stack_drc(self, drc_uri: str, stack: Stack) -> str:
        """
        Update the link from the Stack -> DockerCompose model.abs

        Arguments:
            drc_uri: URI of the previously created DockerCompose
            stack: Stack to link the DockerCompose.
        """
        stack_uuid = stack.uuid if isinstance(stack, UUIDStack) else await stack.uuid        
        stack_uri = "http://swarm-ui.big-data-europe.eu/resources/stacks/{}".format(stack_uuid)
        return await self.sparql.update("""
            PREFIX swarmui: <http://swarmui.semte.ch/vocabularies/core/>
            DELETE {
                GRAPH {{graph}} {
                    <{{stack_uri}}> swarmui:dockerComposeFile ?s.
                }
            } 
            WHERE {
                GRAPH {{graph}} {
                    OPTIONAL {     
                        <{{stack_uri}}> swarmui:dockerComposeFile ?s.
                    }
                }
            }; 
            INSERT DATA 
            {
                GRAPH {{graph}} {
                    <{{stack_uri}}> swarmui:dockerComposeFile <{{drc_uri}}>.
                }
            }
        """, 
        stack_uri=stack_uri,
        drc_uri=drc_uri)


    async def docker_compose(self, stack: Stack):
        """
        Fetches the docker-compose.yml file from the git url.
        
        Steps:
            - If the stack doesn't already have a drc
            - Clone repo & get drc.yml from file
            - Create drc model in DB with the drc.yml file
            - Create link URI -> drc model  
        """
        # TODO: Add "smart" prediction. If it is a github URL no need
        # to clone the repo, we can just download it raw (faster). The 
        # same for gitlab, aws codecommit, if we manage to somehow know
        # the urls to query from the git url, we can leap over cloning 
        # the repo
        uuid = stack.uuid if isinstance(stack, UUIDStack) else await stack.uuid
        if not await self.ensure_stack_has_drc(uuid):
            project_path = "/data/{}".format(uuid)   
            cmd = await self.run_command(
                "git", 
                "clone", 
                await stack.location, 
                "-b", 
                stack.branch, 
                project_path)
            if cmd == 0: # command finished properly
                async with open(path.join(project_path, 
                                          'docker-compose.yml'), 
                                          mode='r') as f:
                    return await f.read()


    def stack_inserted(self, inserts):
        """
        Filters the list of deltas checking if any
        triple of type Stack has been added, and
        return it's URI in that case.
        """
        stack_uri = [elem['s']['value'] for elem in inserts 
                    if elem['o']['value'] == "http://usefulinc.com/ns/doap#Stack" 
                    and elem['p']['value'] == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"]

        return stack_uri[0] if stack_uri else None


    async def handle_delta_update(self, request):
        """
        Handle an update from the delta service, check if
        that update correspond to a stack and if so, create the
        DockerCompose and link it to the stack.
        """
        try:
            data = await request.json()
        except:
            raise web.HTTPBadRequest(body="invalid json")

        inserts = data['delta'][0]['inserts']
        stack_uri = self.stack_inserted(inserts)

        if stack_uri:
            try:
                stack = URIStack({ 'app': self, 'uri': stack_uri })
                docker_compose = await self.docker_compose(stack)
                drc_uri = await self.create_drc_db(docker_compose, stack)
                await self.update_stack_drc(drc_uri, stack)
                return web.Response(body=json.dumps({
                    "data": {
                        "attributes": {
                            "status": 200,
                            "message": "DockerCompose created and Stack updated"
                        },
                        "type": "mu-docker-compose-handler"
                    }}))
            except AttributeError:
                raise web.HTTPInternalServerError(body=json.dumps({
                    "status": 500,
                    "title": "Stack already has DockerCompose",
                    "detail": "Stack {} already has DockerCompose linked to it".format(stack.uuid)
                }))                    
        return web.Response(text="whatever")


    async def cleanup(self):
        """
        Properly close SPARQL client
        """
        await self.sparql.close()


# Create a new application and set the routes to handle requests
app = Application()
app.router.add_post('/createdrc', app.handle_fetch_drc)
app.router.add_post('/update', app.handle_delta_update)
app.on_cleanup.append(app.cleanup)
