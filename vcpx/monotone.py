# -*- mode: python; coding: utf-8 -*-
# :Progetto: vcpx -- Monotone details
# :Creato:   Tue Apr 12 01:28:10 CEST 2005
# :Autore:   Markus Schiltknecht <markus@bluegap.ch>
# :Autore:   Riccardo Ghetta <birrachiara@tin.it>
# :Licenza:  GNU General Public License
#

"""
This module contains supporting classes for Monotone.
"""

__docformat__ = 'reStructuredText'

from shwrap import ExternalCommand, PIPE, ReopenableNamedTemporaryFile, STDOUT
from source import UpdatableSourceWorkingDir, InvocationError, \
     ChangesetApplicationFailure, GetUpstreamChangesetsFailure
from target import SyncronizableTargetWorkingDir, TargetInitializationFailure
from changes import ChangesetEntry,Changeset
from os.path import exists, join, isdir
from os import renames, access, F_OK
from string import whitespace

MONOTONERC = """\
function get_passphrase(KEYPAIR_ID)
  return "%s"
end
"""

class ExternalCommandChain:
    """
    This class implements command piping, i.e. a chain of
    ExternalCommand, each feeding its stdout to the stdin of next
    command in the chain If a command fails, the chain breaks and
    returns error.

    Note:
    This class implements only a subset of ExternalCommand functionality
    """
    def __init__(self, command, cwd=None):
        self.commandchain =command
        self.cwd = cwd
        self.exit_status = 0

    def execute(self):
        outstr = None
        for cmd in self.commandchain:
            input = outstr
            exc = ExternalCommand(cwd=self.cwd, command=cmd)
            out, err = exc.execute(input=input, stdout=PIPE, stderr=PIPE)
            self.exit_status = exc.exit_status
            if self.exit_status:
                break
            outstr = out.getvalue()
        return out, err

class MonotoneChangeset(Changeset):
    """
    Monotone changesets differ from standard Changeset because:
    1. only the "revision" field is used for eq/ne comparison
    2. have additional properties used to handle history linearization
    """

    def __init__(self, linearized_ancestor, revision):
        """
        Initializes a new MonotoneChangeset. The linearized_ancestor
        parameters is the fake ancestor used for linearization. The
        very first revision tailorized has lin_ancestor==None
        """

        Changeset.__init__(self, revision=revision, date=None, author=None, log="")
        self.lin_ancestor = linearized_ancestor

    def __eq__(self, other):
        return (self.revision == other.revision)

    def __ne__(self, other):
        return (self.revision <> other.revision)

    def __str__(self):
        s = [Changeset.__str__(self)]
        s.append('linearized ancestor: %s' % self.lin_ancestor)
        s.append('real ancestor(s): %s' % ','.join(self.real_ancestors))
        return '\n'.join(s)

    def update(self, real_dates, authors, log, real_ancestors):
        """
        Updates the monotone changeset secondary data
        """
        self.author=".".join(authors)
        self.setLog(log)
        self.date = real_dates[0]
        self.real_dates = real_dates
        self.real_ancestors = real_ancestors

class MonotoneLogParser:
    """
    Obtain and parse a *single* "monotone log" output, reconstructing
    the revision information
    """

    class PrefixRemover:
        """
        Helper class. Matches a prefix, allowing access to the text following
        """
        def __init__(self, str):
            self.str = str
            self.value=""

        def __call__(self, prefix):
            if self.str.startswith(prefix):
                self.value = self.str[len(prefix):].strip()
                return True
            else:
                return False

    # logfile states
    SINGLE = 0  # single line state
    ADD = 1 # in add file/dir listing
    MOD = 2 # in mod file/dir listing
    DEL = 3 # in delete file/dir listing
    REN = 4 # in renamed file/dir listing
    LOG = 5 # in changelog listing
    CMT = 6 # in comment listing

    def __init__(self, repository, working_dir):
        self.working_dir = working_dir
        self.repository = repository

    def parse(self, revision):
        from datetime import datetime

        self.revision=""
        self.ancestors=[]
        self.authors=[]
        self.dates=[]
        self.changelog=""

        cmd = self.repository.command("log", "--db", self.repository.repository,
                                      "--last", "1", "--revision", revision)
        mtl = ExternalCommand(cwd=self.working_dir, command=cmd)
        outstr = mtl.execute(stdout=PIPE)
        if mtl.exit_status:
            raise GetUpstreamChangesetsFailure("monotone log returned status %d" % mtl.exit_status)

        logs = ""
        comments = ""
        state = self.SINGLE
        loglines = outstr[0].getvalue().splitlines()
        for curline in loglines:

            pr = self.PrefixRemover(curline)
            if pr("Revision:"):
                if pr.value != revision:
                    raise GetUpstreamChangesetsFailure("Revision doesn't match. Expected %s, found %s" % revision, pr.value)
                state = self.SINGLE
            elif pr("Ancestor:"):
                if pr.value:
                    self.ancestors.append(pr.value) # cset could be a merge and have multiple ancestors
                state = self.SINGLE
            elif pr("Author:"):
                self.authors.append(pr.value)
                state = self.SINGLE
            elif pr("Date:"):
                    # monotone dates are expressed in ISO8601, always UTC
                    dateparts = pr.value.split('T')
                    assert len(dateparts) >= 2, `dateparts`
                    day = dateparts[0]
                    time = dateparts[1]
                    y,m,d = map(int, day.split(day[4]))
                    hh,mm,ss = map(int, time.split(':'))
                    date = datetime(y,m,d,hh,mm,ss)
                    self.dates.append(date)
                    state = self.SINGLE
            elif pr("Branch:") or pr("Tag"):
                # unused data, just resetting state
                state = self.SINGLE
            elif pr("Deleted files:") or pr("Deleted directories:"):
                state=self.DEL
            elif pr("Renamed files:") or pr("Renamed directories:"):
                state=self.DEL
            elif pr("Added files:") or pr("Added directories:"):
                state=self.ADD
            elif pr("Modified files:") or pr("Modified directories:"):
                state=self.ADD
            elif pr("ChangeLog:"):
                state=self.LOG
            elif pr("Comments:"):
                comments=comments + "Note:\n"
                state=self.CMT
            else:
                # otherwise, it must be a log/comment/changeset entry, or an unknown cert line
                if state == self.SINGLE:
                    # line coming from an unknown cert
                    pass
                elif state == self.LOG:
                    # log line, accumulate string
                    logs = logs + curline + "\n"
                elif state == self.CMT:
                    # comment line, accumulate string
                    comments = comments + curline + "\n"
                else:
                    # parse_cset_entry(mode, chset, curline.strip()) # cset entry, handle
                    pass # we ignore cset info

        # parsing terminated, verify the data
        if len(self.authors)<1 or len(self.dates)<1 or revision=="":
            raise GetUpstreamChangesetsFailure("Error parsing log of revision %s. Missing data" % revision)
        self.changelog = logs + comments

    def convertLog(self, chset):
        self.parse(chset.revision)

        chset.update(real_dates=self.dates,
                     authors=self.authors,
                     log=self.changelog,
                     real_ancestors=self.ancestors)

        return chset

class MonotoneDiffParser:
    """
    This class obtains a diff beetween two arbitrary revisions, parsing
    it to get changeset entries.

    Note: since monotone tracks directories implicitly, a fake "add dir"
    cset entry is generated when a file is added to a subdir
    """

    class BasicIOTokenizer:
        # To write its control files, monotone uses a format called
        # internally "basic IO", a stanza file format with items
        # separated by blank lines. Lines are terminated by newlines.
        # The format supports strings, sequence of chars contained by
        # ". String could contain newlines and to insert a " in the
        # middle you escape it with \ (and \\ is used to obtain the \
        # char itself) basic IO files are always UTF-8
        # This class implements a small tokenizer for basic IO

        def __init__(self, stream):
            self.stream = stream

        def _string_token(self):
            # called at start of string, returns the complete string
            # Note: Exceptions checked outside
            escape = False
            str=['"']
            while True:
                ch = self.it.next()
                if escape:
                    escape=False
                    str.append(ch)
                    continue
                elif ch=='\\':
                    escape=True
                    continue
                else:
                    str.append(ch)
                    if ch=='"':
                        break   # end of filename string
            return "".join(str)

        def _normal_token(self, startch):
            # called at start of a token, stops at first whitespace
            # Note: Exceptions checked outside
            tok=[startch]
            while True:
                ch = self.it.next()
                if ch in whitespace:
                    break
                tok.append(ch)

            return "".join(tok)

        def __iter__(self):
            # restart the iteration
            self.it = iter(self.stream)
            return self

        def next(self):
            token =""
            while True:
                ch = self.it.next() # here we just propagate the StopIteration ...
                if ch in whitespace or ch=='#':
                    continue  # skip spaces beetween tokens ...
                elif ch == '"':
                    try:
                        token = self._string_token()
                        break
                    except StopIteration:
                        # end of stream reached while in a string: Error!!
                        raise GetUpstreamChangesetsFailure("diff end while in string parsing.")
                else:
                    token = self._normal_token(ch)
                    break
            return token

    def __init__(self, repository, working_dir):
        self.working_dir = working_dir
        self.repository = repository

    def convertDiff(self, chset):
        """
        Fills a chset with the details data coming by a diff beetween
        chset lin_ancestor and revision (i.e. the linearized history)
        """
        if (not chset.lin_ancestor or
            not chset.revision or
            chset.lin_ancestor == chset.revision):
            raise GetUpstreamChangesetsFailure(
                "Internal error: MonotoneDiffParser.convertDiff called "
                "with invalid parameters: lin_ancestor %s, revision %s" %
                (chset.lin_ancestor, chset.revision))

        # the order of revisions is very important. Monotone gives a
        # diff from the first to the second
        cmd = self.repository.command("diff",
                                      "--db", self.repository.repository,
                                      "--revision", chset.lin_ancestor,
                                      "--revision", chset.revision)

        mtl = ExternalCommand(cwd=self.working_dir, command=cmd)
        outstr = mtl.execute(stdout=PIPE)
        if mtl.exit_status:
            raise GetUpstreamChangesetsFailure(
                "monotone diff returned status %d" % mtl.exit_status)

        # monotone diffs are prefixed by a section containing
        # metainformations about files
        # The section terminates with the first file diff, and each
        # line is prepended by the patch comment char (#).
        tk = self.BasicIOTokenizer(outstr[0].getvalue())
        tkiter = iter(tk)
        in_item = False
        try:
            while True:
                token = tkiter.next()
                if token.startswith("========"):
                    # found first patch marker. Changeset info terminated
                    in_item = False
                    break
                else:
                    in_item = False
                    # now, next token should be a filename
                    fname = tkiter.next()
                    if fname[0] != '"':
                        raise GetUpstreamChangesetsFailure(
                            "Unexpected token sequence: '%s' "
                            "followed by '%s'" %(token, fname))

                    # ok, is a file, control changesets data
                    if token == "add_file" or token=="add_directory":
                        chentry = chset.addEntry(fname[1:-1], chset.revision)
                        chentry.action_kind = chentry.ADDED
                    elif token == "delete_file" or token=="delete_directory":
                        chentry = chset.addEntry(fname[1:-1], chset.revision)
                        chentry.action_kind = chentry.DELETED
                    elif token == "rename_file" or token=="rename_directory":
                        # renames are in the form:  oldname to newname
                        tow = tkiter.next()
                        newname = tkiter.next()
                        if tow != "to" or fname[0]!='"':
                            raise GetUpstreamChangesetsFailure(
                                "Unexpected rename token sequence: '%s' "
                                "followed by '%s'" %(tow, newname))
                        chentry = chset.addEntry(newname[1:-1], chset.revision)
                        chentry.action_kind = chentry.RENAMED
                        chentry.old_name= fname[1:-1]
                    elif token == "patch":
                        # patch entries are in the form: from oldrev to newrev
                        fromw = tkiter.next()
                        oldr = tkiter.next()
                        tow = tkiter.next()
                        newr = tkiter.next()
                        if fromw != "from" or tow != "to":
                            raise GetUpstreamChangesetsFailure(
                                "Unexpected patch token sequence: '%s' "
                                "followed by '%s','%s','%s'" % (fromw, oldr,
                                                                tow, newr))

                        # patch entries are generated also for files
                        # added, so we must ignore the entry if
                        # already present
                        if len( [e for e in chset.entries if e.name==fname[1:-1]])==0:
                            # is a real update
                            chentry = chset.addEntry(fname[1:-1], chset.revision)
                            chentry.action_kind = chentry.UPDATED

        except StopIteration:
            if in_item:
                raise GetUpstreamChangesetsFailure("Unexpected end of 'diff' parsing changeset info")


class MonotoneRevToCset:
    """
    This class is used to create changesets from revision ids.

    Since most backends (and tailor itself) doesn't support monotone
    multihead feature, sometimes we need to linearize the revision
    graph, creating syntethized (i.e. fake) edges beetween revisions.

    The revision itself is real, only its ancestors (and all changes
    beetween) are faked.

    To properly do this, changeset are created by a mixture of 'log'
    and 'diff' output. Log gives the revision data, diff the
    differences beetween revisions.

    Monotone also supports multiple authors/tags/comments for each
    revision, while tailor allows only single values.

    We collapse those multiple data (when present) to single entries
    in the following manner:

    author
      all entries separated by a comma

    date
      chooses only one, at random

    changelog
      all entries appended, without a specific order

    comment
      all comments are appended to the changelog string, prefixed by a
      "Note:" line

    tag
      not used by tailor. Ignored

    branch
      ignored (tailor follows only a single branch)

    testresult
      ignored

    other certs
      ignored

    Changesets created by monotone will have additional fields with
    the original data:

    real_ancestors
      list of the real revision ancestor(s)

    real_dates
      list with all date certs

    lin_ancestor
      linearized ancestor (i.e. previous revision in the linearized history)
    """

    def __init__(self, repository, working_dir):
        self.working_dir = working_dir
        self.repository = repository
        self.logparser = MonotoneLogParser(repository=repository,
                                           working_dir=working_dir)
        self.diffparser = MonotoneDiffParser(repository=repository,
                                             working_dir=working_dir)

    def _cset_from_rev(self, lin_ancestor, revision):
        # prepare a new changeset and fill it with rev data
        chset = MonotoneChangeset(lin_ancestor, revision)
        self.updateCset(chset)
        return chset

    def updateCset(self, chset):
        # Parsing the log fills the changeset from revision data
        self.logparser.convertLog(chset)

        # if an ancestor is available, fills the cset with file/dir entries
        if chset.lin_ancestor:
            self.diffparser.convertDiff(chset)

    def getCset(self, revlist):
        # receives a revlist, already toposorted (i.e. ordered by
        # ancestry) and outputs a list of changesets
        cslist=[]
        anc=revlist[0]
        for r in revlist[1:]:
            cslist.append(self._cset_from_rev(anc, r))
            anc=r
        return cslist


class MonotoneWorkingDir(UpdatableSourceWorkingDir, SyncronizableTargetWorkingDir):

    def convert_head_initial(self, repository, module, revision, working_dir):
        """
        This method handles HEAD and INITIAL pseudo-revisions, converting
        them to monotone revids
        """
        effective_rev = revision
        if revision == 'HEAD' or revision=='INITIAL':
            # in both cases we need the head(s) of the requested branch
            cmd = self.repository.command("automate","heads",
                                          "--db", repository, module)
            mtl = ExternalCommand(cwd=working_dir, command=cmd)
            outstr = mtl.execute(stdout=PIPE)
            if mtl.exit_status:
                raise InvocationError("The branch '%s' is empty" % module)

            revision = outstr[0].getvalue().split()
            if revision == 'HEAD':
                if len(revision)>1:
                    raise InvocationError("Branch '%s' has multiple heads. "
                                          "Please choose only one." % module)
                effective_rev=revision[0]
            else:
                # INITIAL requested. We must get the ancestors of
                # current head(s), topologically sort them and pick
                # the first (i.e. the "older" revision). Unfortunately
                # if the branch has multiple heads then we could end
                # up with only part of the ancestry graph.
                if len(revision)>1:
                    self.log_info("Branch '%s' has multiple heads. There "
                                  "is no guarantee to reconstruct the "
                                  "full history." % module)
                cmd = [ self.repository.command("automate","ancestors",
                                                "--db",repository),
                        self.repository.command("automate","toposort",
                                                "--db",repository, "-@-")
                        ]
                cmd[0].extend(revision)
                cld = ExternalCommandChain(cwd=working_dir, command=cmd)
                outstr = cld.execute()
                if cld.exit_status:
                    raise InvocationError("Ancestor reading returned "
                                          "status %d" % cld.exit_status)
                revision = outstr[0].getvalue().split()
                effective_rev=revision[0]
        return effective_rev

    ## UpdatableSourceWorkingDir

    def _getUpstreamChangesets(self, sincerev=None):
        # monotone descendents returns results sorted in alpha order
        # here we want ancestry order, so descendents output is feed back to
        # mtn for a toposort ...
        cmd = [ self.repository.command("automate","descendents",
                                        "--db", self.repository.repository,
                                        sincerev),
                self.repository.command("automate","toposort",
                                        "--db", self.repository.repository,
                                        "-@-")
                ]
        cld = ExternalCommandChain(cwd=self.repository.rootdir, command=cmd)
        outstr = cld.execute()
        if cld.exit_status:
            raise InvocationError("monotone descendents returned "
                                  "status %d" % cld.exit_status)

        # now childs is a list of revids, we must transform it in a
        # list of monotone changesets at this time we fill only the
        # linearized ancestor and revision ids, because at this time
        # we need only to know WICH changesets must be applied to the
        # target repo, not WHAT are the changesets
        childs = outstr[0].getvalue().split()
        chlist = []
        lin_anc=sincerev
        for r in childs:
            chlist.append(MonotoneChangeset(lin_anc, r))
            lin_anc = r
        return chlist

    def _applyChangeset(self, changeset):
        cmd = self.repository.command("update", "--revision", changeset.revision)
        mtl = ExternalCommand(cwd=self.basedir, command=cmd)
        mtl.execute()
        if mtl.exit_status:
            raise ChangesetApplicationFailure("'mtn update' returned "
                                              "status %s" % mtl.exit_status)
        mtr = MonotoneRevToCset(repository=self.repository,
                                working_dir=self.basedir)
        mtr.updateCset( changeset )

        return False   # no conflicts

    def _checkoutUpstreamRevision(self, revision):
        """
        Concretely do the checkout of the FIRST upstream revision.
        """
        effrev = self.convert_head_initial(self.repository.repository,
                                           self.repository.module, revision,
                                           self.basedir)
        if not exists(join(self.basedir, 'MT')):
            self.log_info("checking out a working copy")
            cmd = self.repository.command("co",
                                          "--db", self.repository.repository,
                                          "--revision", effrev,
                                          "--branch", self.repository.module,
                                          self.repository.subdir)
            mtl = ExternalCommand(cwd=self.repository.rootdir, command=cmd)
            mtl.execute()
            if mtl.exit_status:
                raise TargetInitializationFailure(
                    "'monotone co' returned status %s" % mtl.exit_status)
        else:
            self.log_info("%s already exists, assuming it's a monotone "
                          "working dir" % self.basedir)

        # Ok, now the workdir contains the checked out revision. We
        # need to return a changeset describing it.  Since this is the
        # first revision checked out, we don't have a (linearized)
        # ancestor, so we must use None as the lin_ancestor parameter
        chset = MonotoneChangeset(None, effrev)

        # now we update the new chset with basic data - without the
        # linearized ancestor, changeset entries will NOT be filled
        mtr = MonotoneRevToCset(repository=self.repository,
                                working_dir=self.basedir)
        mtr.updateCset(chset)
        return chset

    ## SyncronizableTargetWorkingDir

    def _addPathnames(self, names):
        """
        Add some new filesystem objects, skipping directories (directory
        addition is implicit in monotone)
        """
        fnames=[]
        for fn in names:
            if isdir(join(self.basedir, fn)):
                self.log_info("ignoring addition of directory '%s' (%s)" %
                              (fn, join(self.basedir, fn)) )
            else:
                fnames.append(fn)
        if len(fnames):
            # ok, we still have something to add
            cmd = self.repository.command("add")
            add = ExternalCommand(cwd=self.basedir, command=cmd)
            add.execute(fnames)
            if add.exit_status:
                raise ChangesetApplicationFailure("%s returned status %s" %
                                                  (str(add),add.exit_status))


    def _addSubtree(self, subdir):
        """
        Add a whole subtree
        """
        cmd = self.repository.command("add")
        add = ExternalCommand(cwd=self.basedir, command=cmd)
        add.execute(subdir)
        if add.exit_status:
            raise ChangesetApplicationFailure("%s returned status %s" %
                                              (str(add),add.exit_status))

    def _commit(self, date, author, patchname, changelog=None, entries=None):
        """
        Commit the changeset.
        """

        from sys import getdefaultencoding

        encoding = ExternalCommand.FORCE_ENCODING or getdefaultencoding()

        logmessage = []
        if patchname:
            logmessage.append(patchname.encode(encoding))
        if changelog:
            logmessage.append(changelog.encode(encoding))

        rontf = ReopenableNamedTemporaryFile('mtn', 'tailor')
        log = open(rontf.name, "w")
        log.write('\n'.join(logmessage))
        log.close()

        cmd = self.repository.command("commit", "--author", author,
                                      "--date", date.isoformat(),
                                      "--message-file", rontf.name)
        commit = ExternalCommand(cwd=self.basedir, command=cmd)

        if not entries:
            entries = ['.']

        output, error = commit.execute(entries, stdout=PIPE, stderr=PIPE)

        # monotone complaints if there are no changes from the last commit.
        # we ignore those errors ...
        if commit.exit_status:
            text = error.read()
            if text.find("monotone: misuse: no changes to commit") == -1:
                self.log_error(text)
                raise ChangesetApplicationFailure(
                    "%s returned status %s" % (str(commit),commit.exit_status))
            else:
                self.log_info("No changes to commit - changeset ignored")

    def _removePathnames(self, names):
        """
        Remove some filesystem object.
        """

        # Monotone currently doesn't allow removing a directory, so we
        # must remove every item separately and intercept monotone
        # directory errore messages.  We can't just filter the
        # directories, because the wc doesn't contain them anymore ...
        cmd = self.repository.command("drop")
        drop = ExternalCommand(cwd=self.basedir, command=cmd)
        for fn in names:
            dum, error = drop.execute(fn, stderr=PIPE)
            if drop.exit_status:
                if not error.read().find("drop <directory>"):
                    self.log_error(error.read())
                    raise ChangesetApplicationFailure("%s returned status %s" %
                                                      (str(drop),
                                                       drop.exit_status))

    def _renamePathname(self, oldname, newname):
        """
        Rename a filesystem object.
        """
        # this function is called *after* the file/dir has changed name,
        # and monotone doesn't like it.
        # we put names back to make it happy ...
        if access(join(self.basedir, newname), F_OK):
            if access(join(self.basedir, oldname), F_OK):
                raise ChangesetApplicationFailure("Can't rename %s to %s. "
                                                  "Both names already exist" %
                                                  (oldname, newname))
            renames(join(self.basedir, newname), join(self.basedir, oldname))
            self.log_info("preparing to rename %s->%s" % (oldname, newname))

        cmd = self.repository.command("rename")
        rename = ExternalCommand(cwd=self.basedir, command=cmd)
        rename.execute(oldname, newname)

        # redo the rename ...
        renames(join(self.basedir, oldname), join(self.basedir, newname))
        if rename.exit_status:
            raise ChangesetApplicationFailure("%s returned status %s" %
                                              (str(rename),rename.exit_status))

    def __createRepository(self, target_repository):
        """
        Create a new monotone DB, storing the commit keys, if available
        """

        cmd = self.repository.command("db", "init", "--db",
                                      target_repository.repository)
        init = ExternalCommand(command=cmd)
        init.execute()

        if init.exit_status:
            raise TargetInitializationFailure("Was not able to initialize "
                                              "the monotone db at %r" %
                                              target_repository)

        if target_repository.keyfile:
            # a key file is available, read into the database
            keyfile = file(target_repository.keyfile)
            cmd = self.repository.command("read", "--db",
                                          target_repository.repository)
            regkey = ExternalCommand(command=cmd)
            regkey.execute(input=keyfile)
        else:
            # no keyfile specified, generate a new key - if a
            # passphrase is defined, automatically provide it The
            # keyid must be available
            if not target_repository.keyid:
                raise TargetInitializationFailure("Can't setup the monotone "
                                                  "repository %r. "
                                                  "A keyfile or keyid must "
                                                  "be provided." %
                                                  target_repository)
            cmd = self.repository.command("genkey", "--db",
                                          target_repository.repository)
            regkey = ExternalCommand(command=cmd)
            if target_repository.passphrase:
                passp="%s\n%s\n" % (target_repository.passphrase,
                                    target_repository.passphrase)
            regkey.execute(target_repository.keyid, input=passp)

        if regkey.exit_status:
            raise TargetInitializationFailure("Was not able to setup "
                                              "the monotone initial key at %r" %
                                              target_repository)

    def _prepareTargetRepository(self):
        """
        Check for target repository existence, eventually create it.
        """

        from os.path import exists

        if not self.repository.repository:
            return

        if not exists(self.repository.repository):
            self.__createRepository(self.repository)

    def _prepareWorkingDirectory(self, source_repo):
        """
        Possibly checkout a working copy of the target VC, that will host the
        upstream source tree, when overriden by subclasses.
        """

        from os.path import join, exists

        if not self.repository.repository or exists(join(self.basedir, 'MT')):
            return

        cmd = self.repository.command("setup",
                                      "--db", self.repository.repository,
                                      "--branch", self.repository.module)

        if not self.repository.module:
            raise TargetInitializationFailure("Monotone needs a module "
                                              "defined (to be used as "
                                              "commit branch)")

        setup = ExternalCommand(command=cmd)
        setup.execute(self.basedir)

        if self.repository.passphrase:
            monotonerc = open(join(self.basedir, 'MT', 'monotonerc'), 'w')
            monotonerc.write(MONOTONERC % self.repository.passphrase)
            monotonerc.close()

    def _initializeWorkingDir(self):
        """
        Setup the monotone working copy

        The user must setup a monotone working directory himself or use the
        tailor config file to provide parameters for creation. Then
        we simply use 'monotone commit', without having to specify a database
        file or branch. Monotone looks up the database and branch in it's MT
        directory.
        """

        if not exists(join(self.basedir, 'MT')):
            raise TargetInitializationFailure("Please setup '%s' as a "
                                              "monotone working directory" %
                                              self.basedir)

        SyncronizableTargetWorkingDir._initializeWorkingDir(self)
