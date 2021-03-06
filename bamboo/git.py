# coding: utf-8
import shutil
import os
import sys

from bamboo.helpers import parse_config, tuple_version, cerr, query_yes_no, cout
from bamboo.mixins import BuildMixin


class GitError(Exception):
    pass


class GitHelper(BuildMixin):
    """ Работа с JIRA-задачами в GIT."""

    def __init__(self, project_key, configfile='bamboo.cfg', root=None,
                 temp_dir='/tmp', big_bang_version='0.0.0'):
        self.project_key = project_key
        self.remote_name = "origin"
        self.project_root = root
        self.temp_dir = temp_dir
        self.big_bang_version = big_bang_version
        parse_config(self, configfile)
        self.branches_to_delete = set()

    def rc_tag(self, version, build_number):
        """ название тега для релиз кандидата """
        return "{version}-{build_number}".format(
            version=version, build_number=build_number)

    def release_tag(self, version):
        """ название тега для финального релиза """
        return version

    def remote(self, branch):
        """ возвращает имя удаленной ветки """
        return "%s/%s" % (self.remote_name, branch)

    def git(self, args, quiet=False):
        if not isinstance(args, tuple):
            args = tuple(args)
        args = (
            ('/usr/bin/env', 'git') + args
        )

        stdout, stderr, returncode = self.execute(args, quiet)
        if returncode != 0:
            cerr(stdout)
            cerr(stderr)
            raise GitError()
        return stdout

    def _calc_version(self, version, operator):
        v = tuple_version(version)
        if v <= tuple_version(self.big_bang_version):
            raise GitError("Invalid vesion number %s" % version)

        new_version = list(reversed(v))
        for i, n in enumerate(new_version):
            if n > 0:
                new_version[i] = operator(n)
                break

        return ".".join(str(i) for i in reversed(new_version))

    def previous_version(self, version):
        """ Возвращает предыдущую версию для релиза.
        Например, для релиза 1.0.0 - предыдущая версия 0.0.0
                             1.2.1 - 1.2.0
                             1.2.2 - 1.2.1
                             1.1.0 - 1.0.0
        """
        return self._calc_version(version, operator=lambda a: a - 1)

    def next_version(self, version):
        """ Возвращает следующую версию для релиза.
        Например, для релиза 1.0.0 - 2.0.0
                             1.2.1 - 1.2.2
                             1.2.2 - 1.2.3
                             1.1.0 - 1.2.0
        """
        return self._calc_version(version, operator=lambda a: a + 1)

    def base_version(self, version):
        """ Возвращает базовую версию для релиза, т.е. ту, от которой
        ветка релиза взяла начало
        Например: 1.0.0 - 0.0.0
                  1.0.1 - 1.0.0
                  1.0.2 - 1.0.0
                  1.2.0 - 1.0.0
                  2.1.3 - 2.1.0
        """
        return self._calc_version(version, operator=lambda a: 0)

    def check_version(self, version):
        """ Проверяет, что мы можем собирать указанную версию релиза.
        Например, мы не можем собирать мажор 3.0.0 пока ещё не закрыт мажор
        2.0.0 или когда уже началась сборка мажора 4.0.0

        :param version: версия для проверки
        """
        cerr("Checking version %s before release" % version)
        # не можем собрать тег, если текущая версия уже зарелизена
        if self.find_tags(self.release_tag(version)):
            raise GitError("Cannot add features to %s version because it has "
                           "already released" % version)

        prev_version = self.previous_version(version)
        # Не можем создать релиз, если ещё не зарелизена окончательно предыдущая
        # версия, за исключением случаев, если это вообще первая версия
        if (prev_version != self.big_bang_version and
                not self.find_tags(self.release_tag(prev_version))):
            raise GitError("Cannot create %s release because previous "
                           "%s release does not exist" % (version, prev_version))

        next_version = self.next_version(version)
        # Если для следующей версии (той, что использует тот же стейбл)
        # была уже хоть одна сборка - не можем создать релиз
        if self.find_tags(self.rc_tag(next_version, "*")):
            raise GitError("Cannot create %s release because %s release "
                           "already started" % (version, next_version))
        cerr("Checking complete")

    def is_minor_release(self, version):
        """ Определеяет минорный ли это релиз.
        """
        return tuple_version(version)[1:] > (0, 0)

    def get_stable_branch(self, version):
        """ Возвращает название ветки для сборки релиза
        """
        version = tuple_version(version)

        if not self.is_minor_release(version):
            return "master"
        if version[-1] == 0:
            return "minor/%d.x" % version[0]
        else:
            return "minor/%d.%d.x" % version[:2]

    def get_or_create_stable(self, version, task, interactive=False):
        """ Проверяет наличие или создает ветку, в которую будем собирать
        изменения

        :return: Название ветки стейбла
        """
        branch = self.get_stable_branch(version)

        if not self.git(("branch", "--list", branch)):
            if self.git(("branch", "-r", "--list", self.remote(branch))):
                # если на сервере уже есть ветка, то будем использовать ей
                start_point = self.remote(branch)
                cerr("Checkout release branch %s for version %s" % (branch, version))
            else:
                # иначе создадим ветку из релиза предыдущей версии
                start_point = self.release_tag(self.base_version(version))
                cerr("Create release branch %s for version %s" % (branch, version))
            # создаем локальную ветку и связываем её с удаленной. сам по себе
            # чекаут здесь не так уже важен
            self.git(("checkout", "-b", branch, start_point))

        return branch

    def check_task(self, branch, version):
        """ Проверяем, можем ли мы смержить задачу в текущую версию.
        Мы не можем мержить тикет в минор, если при мерже в минор попадут
        какие-то другие коммиты из других версий.
        Например:
        1. Ветка feature начата раньше, чем сделан минор - её можно смержить
        в минор, т.к. в него не попадут никакие коммиты из мастера, сделанные
        не в рамках feature:
        ---1----2------- master
           \    \_______ minor
            \___________ feature

        2. Ветка feature начата позже, чем сделан минор - её нельзя просто так
        смержить в минор, т.к. в него попадут все коммиты, сделанные в мастер
        между коммитами 1 и 2, а мы не хотим их в минор:
        ---1----2------- master
           \    \_______ feature
            \___________ minor

        3. Ветка feature начата в миноре. Её можно спокойно смержить в него:
        ----1--------- master
            \_2_______ minor
              \_______ feature

        4. Ветка feature начата раньше, чем сделан минор, но уже после создания
        минора в неё были вмержен мастер. Поэтому вмержить её в минор нельзя,
        иначе в минор попадут коммиты, сделанные между 2 (созданием минора) и
        3 (мержем из мастера):
        ---1--2--3------- master
           \__|___\______ feature
              \__________ minor
        5. FIXME это пока не работает
        Ветка feature начата раньше, чем сделан минор, но вмержена в
        мастер после создания минора. Её можно смержить в минор,
        т.к. ничего лишнего в него не попадет:
        ---1--2----3------ master
           \__|___/      feature
              \_________ minor
        """
        if not self.is_minor_release(version):
            return

        # ищем общий коммит у ветки для мержа и ветки-родоночальника стейбла
        # (для миноров - это мастер, для патчей - это минор)
        parent_branch = self.get_stable_branch(self.base_version(version))
        stable_branch = self.get_stable_branch(version)
        base = self.git(("merge-base", self.remote(branch), self.remote(parent_branch))).strip()
        self.checkout(stable_branch)
        try:
            self.git(("merge-base", "--is-ancestor", base, stable_branch))
        except GitError:
            raise GitError(
                "Cannot merge {feature} to {version} because unexpected "
                "commits can be merged too. You can rebase {feature} branch on "
                "the begining of {stable} or create new branch originated "
                "from {stable} and cherry-pick nessesary commits to it.".format(
                feature=branch, version=version, stable=stable_branch))

    def merge_tasks(self, task_key, tasks, version):
        """ Мержит задачу из ветки в нужный релиз-репозиторий
        """
        if not tasks:
            raise ValueError('No tasks requested')

        stable_branch = self.get_or_create_stable(version, task=task_key)
        commit_msg = '%s merge tasks %%s' % task_key

        for task in tasks:
            # проверяем, можем ли мы смержить эту таску в стейбл
            self.check_task(task.key, version)
            # мержим ветку в стейбл
            self.merge(task.key, stable_branch, commit_msg % task.key)
            # удаляем ветку сразу после мержа
            self.delete_branch(task.key)

    def find_tags(self, pattern):
        """ Находит все теги для указанного шаблона
        """
        stdout = self.git(("tag", "-l", pattern))
        return stdout.split()

    def get_last_tag(self, version):
        """ Возвращает номер последней сборки
        """
        pattern = self.rc_tag(version, "")
        # текущий - это последний + 1
        tags = [t.replace(pattern, "") for t in self.find_tags(pattern + "*")]
        number_tags = sorted((t for t in tags if t.isdigit()), key=int)
        return int(number_tags[-1]) if number_tags else 0

    def release_candidate(self, version):
        """ Помечает тегом релиз кандидата текущий коммит.
        """
        tag = self.rc_tag(version, self.get_last_tag(version) + 1)
        self.git(("tag", tag))
        return tag

    def release(self, version, build_number):
        """ Помечает релиз-тегом указанный билд.
        """
        rc_tag = self.rc_tag(version, build_number)
        tag = self.release_tag(version)
        self.git(("tag", tag, rc_tag))
        return tag

    def clone(self, path):
        """ Клонирует репозиторий по указанному пути и переходит туда
        """
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        self.git(("clone", self.project_root, path))
        os.chdir(path)

    def checkout(self, branch):
        """ Делает checkout указанной ветки
        """
        self.git(("checkout", branch))

    def merge(self, from_branch, to_branch, commit_msg):
        """ Мержит одну ветку в другую
        """
        self.checkout(from_branch)
        self.checkout(to_branch)
        self.git(("merge", "--no-ff", from_branch, "-m", commit_msg))

    def push(self):
        """ Отправляет изменения на удаленный сервер, включая все теги и
        удаление веток, если нужно
        """
        self.git(("push", "--all"))
        self.git(("push", "--tags"))
        for branch in self.branches_to_delete:
            self.delete_remote_branch(branch)
        self.branches_to_delete = set()

    def delete_branch(self, branch, deffer_remote=True):
        """ Удаляет ветку в локальном репе и запоминает, что её
        """
        self.git(("branch", "-d", branch))
        if deffer_remote:
            self.branches_to_delete.add(branch)
        else:
            self.delete_remote_branch(branch)

    def delete_remote_branch(self, branch):
        """ Удаляет ветку в удаленном репозитории
        """
        self.git(("push", self.remote_name, "--delete", branch))

    def build(self, release, interactive=False, build_cmd=None, terminate=False,
              build=None, cleanup=True):
        tag = build or '%02d' % self.get_last_tag(release)
        package_name = '%s-%s-%s' % (self.project_key, release, tag)
        local_path = os.path.join(self.temp_dir, package_name)
        if os.path.exists(local_path):
            if not interactive or query_yes_no('remove %s?' % local_path,
                                               default='yes'):
                shutil.rmtree(local_path)
            else:
                cerr('Aborted')
                sys.exit(0)
        self.clone(local_path)
        self.checkout(self.rc_tag(release, build))
        if build_cmd:
            os.environ['PACKAGE'] = package_name
            os.chdir(local_path)
            cerr("Build cmd: %s" % build_cmd)
            cerr("Package name: %s" % package_name)
            if interactive and not query_yes_no('execute?', default='yes'):
                cerr('Aborted')
                return
            args = ('/usr/bin/env', 'sh', '-c', build_cmd)
            stdout, stderr, ret = self.execute(args)
            cout(stdout)
            if ret:
                cerr(stderr)
                sys.exit(ret)
            if terminate:
                if cleanup:
                    shutil.rmtree(local_path)
                return

        archive_name = os.path.join(self.temp_dir, '%s.tgz' % package_name)
        self.tar(archive_name, self.temp_dir, package_name, quiet=True)
        dest = os.path.join(self.repo_url, self.project_key)
        if not dest.endswith('/'):
            dest += '/'
        self.upload(archive_name, dest, interactive=interactive)
        if cleanup:
            cout("cleanup")
            shutil.rmtree(local_path)
            os.unlink(archive_name)