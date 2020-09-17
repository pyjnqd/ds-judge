import asyncio
import collections
import datetime
import io
import os
import pytz
import yaml
import zipfile
from bson import objectid
from aiofile import AIOFile

from vj4 import app
from vj4 import constant
from vj4 import error
from vj4.model import builtin
from vj4.model import document
from vj4.model import record
from vj4.model import user
from vj4.model import domain
from vj4.model.adaptor import discussion
from vj4.model.adaptor import contest
from vj4.model.adaptor import problem
from vj4.handler import base
from vj4.util import pagination

from aiohttp import web
from vj4.db import mdb
from vj4.model import fs
from vj4.model.report import report_rename
from vj4.model.adaptor.setting import Setting


# @app.route('/report', 'report_main')
# class ReportHandler(base.Handler):
#   @base.require_priv(builtin.PRIV_USER_PROFILE)
#   async def get(self):
#     self.render('test.html', user="chuyang", age=20)
#
#
# @app.route('/report/{rid}', 'report_rid')
# class ReportSecondHandler(base.Handler):
#   @base.route_argument
#   @base.require_priv(builtin.PRIV_USER_PROFILE)
#   @base.get_argument
#   async def get(self, *, rid: int=0, page: int=1):
#     self.render('test.html', user="chuyang", age=20, msg=rid, page=page)

@app.route('/report', 'report_main')
class HomeworkMainHandler(contest.ContestMixin, base.Handler):
  @base.require_perm(builtin.PERM_VIEW_HOMEWORK)
  async def get(self):
    reports = list(mdb['report'].find().sort('report_id'))
    calendar_reports = []
    for report in reports:
      cal_report = {
        'id': report['report_id'],
        'begin_at': self.datetime_stamp(report['begin_at']),
        'title': report['title'],
        'status': self.status_text(report),
        'end_at': self.datetime_stamp(report['end_at']),
        'url': self.reverse_url('report_detail', rid=report['_id']),
      }
      calendar_reports.append(cal_report)

    self.render('report_main.html', tdocs=reports, calendar_tdocs=calendar_reports)

    # tdocs = await contest.get_multi(self.domain_id, document.TYPE_HOMEWORK).to_list()
    #
    # calendar_tdocs = []
    # for tdoc in tdocs:
    #   cal_tdoc = {'id': tdoc['doc_id'],
    #               'begin_at': self.datetime_stamp(tdoc['begin_at']),
    #               'title': tdoc['title'],
    #               'status': self.status_text(tdoc),
    #               'url': self.reverse_url('homework_detail', tid=tdoc['doc_id'])}
    #   if self.is_homework_extended(tdoc) or self.is_done(tdoc):
    #     cal_tdoc['end_at'] = self.datetime_stamp(tdoc['end_at'])
    #     cal_tdoc['penalty_since'] = self.datetime_stamp(tdoc['penalty_since'])
    #   else:
    #     cal_tdoc['end_at'] = self.datetime_stamp(tdoc['penalty_since'])
    #   calendar_tdocs.append(cal_tdoc)


@app.route('/report/create', 'report_create')
class ReportCreateHandler(contest.ContestMixin, base.Handler):
  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.require_perm(builtin.PERM_CREATE_HOMEWORK)
  async def get(self):
    begin_at = self.now.replace(tzinfo=pytz.utc).astimezone(self.timezone) + datetime.timedelta(days=1)
    penalty_since = begin_at + datetime.timedelta(days=7)
    page_title = self.translate('Report create')
    path_components = self.build_path((page_title, None))
    self.render('report_edit.html',
                course=0,
                report_id=1,
                date_begin_text=begin_at.strftime('%Y-%m-%d'),
                time_begin_text='00:00',
                date_penalty_text=penalty_since.strftime('%Y-%m-%d'),
                time_penalty_text='23:59',
                pids=contest._format_pids([1000, 1001]),
                extension_days='0',
                page_title=page_title, path_components=path_components)

  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.require_perm(builtin.PERM_EDIT_PROBLEM)
  @base.require_perm(builtin.PERM_CREATE_HOMEWORK)
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, report_id: int, course: int, title: str, content: str,
                 begin_at_date: str, begin_at_time: str,
                 penalty_since_date: str, penalty_since_time: str):
    try:
      begin_at = datetime.datetime.strptime(begin_at_date + ' ' + begin_at_time, '%Y-%m-%d %H:%M')
      begin_at = self.timezone.localize(begin_at).astimezone(pytz.utc).replace(tzinfo=None)
    except ValueError:
      raise error.ValidationError('begin_at_date', 'begin_at_time')
    try:
      penalty_since = datetime.datetime.strptime(penalty_since_date + ' ' + penalty_since_time, '%Y-%m-%d %H:%M')
      penalty_since = self.timezone.localize(penalty_since).astimezone(pytz.utc).replace(tzinfo=None)
    except ValueError:
      raise error.ValidationError('end_at_date', 'end_at_time')
    end_at = penalty_since
    if begin_at >= penalty_since:
      raise error.ValidationError('end_at_date', 'end_at_time')
    if penalty_since > end_at:
      raise error.ValidationError('extension_days')

    current_year = datetime.datetime.now().year
    rid = objectid.ObjectId()
    mdb.report.insert_one({
      "_id": rid,
      "report_id": report_id,
      "title": title,
      "content": content,
      "year": current_year,
      "course": course,
      "begin_at": begin_at,
      "end_at": end_at,
    })
    # tid = await contest.add(self.domain_id, document.TYPE_HOMEWORK, title, content, self.user['_id'],
    #                         constant.contest.RULE_ASSIGNMENT, begin_at, end_at, pids,
    #                         penalty_since=penalty_since, penalty_rules=penalty_rules)
    self.json_or_redirect(self.reverse_url('report_detail', rid=rid))


@app.route('/report/{rid:\w{24}}', 'report_detail')
class ReportDetailHandler(contest.ContestMixin, base.OperationHandler):
  DISCUSSIONS_PER_PAGE = 15

  @base.route_argument
  @base.require_perm(builtin.PERM_VIEW_HOMEWORK)
  @base.get_argument
  @base.sanitize
  async def get(self, *, rid: objectid.ObjectId, page: int = 1):
    report = mdb.report.find_one({'_id': rid})

    ureport = mdb.ureport.find_one({'report_id': report['_id'], 'user_id': self.user['_id']})


    # tsdoc, pdict = await asyncio.gather(
    #     contest.get_status(self.domain_id, document.TYPE_HOMEWORK, tdoc['doc_id'], self.user['_id']),
    #     problem.get_dict(self.domain_id, tdoc['pids']))
    # psdict = dict()
    # rdict = dict()
    # if tsdoc:
    #   attended = tsdoc.get('attend') == 1
    #   for pdetail in tsdoc.get('detail', []):
    #     psdict[pdetail['pid']] = pdetail
    #   if self.can_show_record(tdoc):
    #     rdict = await record.get_dict((psdoc['rid'] for psdoc in psdict.values()),
    #                                   get_hidden=True)
    #   else:
    #     rdict = dict((psdoc['rid'], {'_id': psdoc['rid']}) for psdoc in psdict.values())
    # else:
    #   attended = False
    # # discussion
    # ddocs, dpcount, dcount = await pagination.paginate(
    #     discussion.get_multi(self.domain_id,
    #                          parent_doc_type=tdoc['doc_type'],
    #                          parent_doc_id=tdoc['doc_id']),
    #     page, self.DISCUSSIONS_PER_PAGE)
    # uids = set(ddoc['owner_uid'] for ddoc in ddocs)
    # uids.add(tdoc['owner_uid'])
    # udict = await user.get_dict(uids)
    # dudict = await domain.get_dict_user_by_uid(domain_id=self.domain_id, uids=uids)
    # path_components = self.build_path(
    #   (self.translate('homework_main'), self.reverse_url('homework_main')),
    #   (tdoc['title'], None))

    # self.render("test.html")

    self.render(
      'report_detail.html',
      tdoc=report,
      pdoc=ureport,
      attended=True,
      page=page,
      datetime_stamp=self.datetime_stamp,
      page_title=report['title'],
    )

  @base.route_argument
  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.require_perm(builtin.PERM_ATTEND_HOMEWORK)
  @base.require_csrf_token
  @base.sanitize
  async def post_attend(self, *, tid: objectid.ObjectId):
    tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
    if self.is_done(tdoc):
      raise error.HomeworkNotLiveError(tdoc['doc_id'])
    await contest.attend(self.domain_id, document.TYPE_HOMEWORK, tdoc['doc_id'], self.user['_id'])
    self.json_or_redirect(self.url)


@app.route('/report/{rid}/download', 'report_download')
class ReportDownloadHandler(contest.ContestMixin, base.Handler):
  @base.route_argument
  @base.post_argument
  @base.require_csrf_token
  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.require_perm(builtin.PERM_ATTEND_HOMEWORK)
  @base.sanitize
  async def post(self, rid: objectid.ObjectId):
    user_id = self.user['_id']
    ureport = mdb.ureport.find_one({'user_id': user_id, 'report_id': rid})
    file_name = ureport['data']
    with open('data/%s' % file_name, 'rb') as fb:
      await self.binary(fb.read(), file_name=file_name)


@app.route('/report/{rid}/upload', 'report_upload')
class ReportUploadHandler(base.Handler):
  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.route_argument
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, rid: str):
    data = await self.request.post()
    file = data.get('file')
    file_data = file.file
    file_name = file.filename
    file_extension = file_name.split(".")[-1]

    def check_file_type(extension):
      if extension not in ['pdf', 'doc', 'docx']:
        raise error.FileTypeNotAllowedError(extension)

    check_file_type(file_extension)
    uid = self.user['_id']
    upload_time = pytz.utc.localize(datetime.datetime.now()).astimezone(self.timezone)
    student = mdb.user.find_one({"_id": uid})
    rid = objectid.ObjectId(rid)
    this_report = mdb.report.find_one({"_id": rid})
    report_id = this_report['_id']

    report_deadline = pytz.utc.localize(this_report['end_at']).astimezone(self.timezone)
    if upload_time > report_deadline:
      raise error.HomeworkNotLiveError()

    if report_rename(student, this_report, file_extension) is not None:
      file_name = report_rename(student, this_report, file_extension)

    ureport = mdb.ureport.find_one({'user_id': uid, 'report_id': report_id})
    pdoc = None
    if ureport:
      try:
        os.unlink('data/%s' % ureport['data'])
      except FileNotFoundError as e:
        pass
      pdoc = mdb.ureport.update_one({'user_id': uid, 'report_id': report_id}, {"$set": {"data": file_name, "upload_time": upload_time}})
    else:
      pdoc = mdb.ureport.insert_one({
        '_id': objectid.ObjectId(),
        'user_id': uid,
        'report_id': report_id,
        'data': file_name,
        'upload_time': upload_time})

    buffer = io.BytesIO(file_data.read())

    async with AIOFile('data/%s' % file_name, 'wb') as fb:
      await fb.write(buffer.getvalue())

    # if report.report_rename(student, report) is not None:
    #   file_name = report.report_rename(student, report)
    # ureport = mdb.find_one({'user_id': uid, 'report_id': report_id})
    # print(file_name)


    # if ureport and ureport['data'] is objectid.ObjectId:
    #   await fs.unlink(ureport['data'])


    # pdoc = await problem.get(self.domain_id, pid)
    # if not self.own(pdoc, builtin.PERM_EDIT_PROBLEM_SELF):
    #   self.check_perm(builtin.PERM_EDIT_PROBLEM)
    # if (not self.own(pdoc, builtin.PERM_READ_PROBLEM_DATA_SELF)
    #     and not self.has_perm(builtin.PERM_READ_PROBLEM_DATA)):
    #   self.check_priv(builtin.PRIV_READ_PROBLEM_DATA)
    # if pdoc.get('data') and type(pdoc['data']) is objectid.ObjectId:
    #   await fs.unlink(pdoc['data'])
    # await problem.set_data(self.domain_id, pid, file)
    self.json_or_redirect(self.reverse_url('report_detail', rid=rid))


@app.route('/report/{rid:\w{24}}/edit', 'report_edit')
class ReportEditHandler(contest.ContestMixin, base.Handler):
  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.require_perm(builtin.PERM_EDIT_HOMEWORK)
  @base.route_argument
  @base.sanitize
  async def get(self, *, rid: objectid.ObjectId):
    tdoc = mdb.report.find_one({'_id': rid})
    # if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
    self.check_perm(builtin.PERM_EDIT_HOMEWORK)
    begin_at = pytz.utc.localize(tdoc['begin_at']).astimezone(self.timezone)
    # penalty_since = pytz.utc.localize(tdoc['end_at']).astimezone(self.timezone)
    end_at = pytz.utc.localize(tdoc['end_at']).astimezone(self.timezone)
    # extension_days = round((end_at - penalty_since).total_seconds() / 60 / 60 / 24, ndigits=2)
    page_title = self.translate('report_edit')
    path_components = self.build_path(
      (self.translate('report_main'), self.reverse_url('report_main')),
      (tdoc['title'], self.reverse_url('report_detail', rid=tdoc['_id'])),
      (page_title, None))
    self.render('report_edit.html', tdoc=tdoc,
                date_begin_text=begin_at.strftime('%Y-%m-%d'),
                time_begin_text=begin_at.strftime('%H:%M'),
                date_penalty_text=end_at.strftime('%Y-%m-%d'),
                time_penalty_text=end_at.strftime('%H:%M'),
                report_id=tdoc['report_id'],
                course=tdoc['course'],
                pids=[1000, 1001],
                page_title=page_title, path_components=path_components)

  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.require_perm(builtin.PERM_EDIT_PROBLEM)
  @base.require_perm(builtin.PERM_CREATE_HOMEWORK)
  @base.post_argument
  @base.route_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, rid: objectid.ObjectId, title: str, content: str,
                 course: int, report_id: int,
                 begin_at_date: str, begin_at_time: str,
                 penalty_since_date: str, penalty_since_time: str):

    tdoc = mdb.report.find_one({"_id": rid})
    self.check_perm(builtin.PERM_EDIT_HOMEWORK)
    try:
      begin_at = datetime.datetime.strptime(begin_at_date + ' ' + begin_at_time, '%Y-%m-%d %H:%M')
      begin_at = self.timezone.localize(begin_at).astimezone(pytz.utc).replace(tzinfo=None)
    except ValueError:
      raise error.ValidationError('begin_at_date', 'begin_at_time')
    try:
      penalty_since = datetime.datetime.strptime(penalty_since_date + ' ' + penalty_since_time, '%Y-%m-%d %H:%M')
      penalty_since = self.timezone.localize(penalty_since).astimezone(pytz.utc).replace(tzinfo=None)
    except ValueError:
      raise error.ValidationError('end_at_date', 'end_at_time')
    end_at = penalty_since
    if begin_at >= penalty_since:
      raise error.ValidationError('end_at_date', 'end_at_time')
    if penalty_since > end_at:
      raise error.ValidationError('extension_days')

    mdb.report.update_one({'_id': rid},
                          {'$set':
                             {'begin_at': begin_at,
                              'end_at': end_at,
                              'report_id': report_id,
                              'course': course,
                              'title': title,
                              'content': content}
                           })

    pdoc = mdb.ureport.find_one({'user_id': self.user['_id'], 'report_id': rid})
    self.json_or_redirect(self.reverse_url('report_detail', rid=rid))


@app.route('/report/{rid}/gather', 'report_gather')
class ReportCodeHandler(contest.ContestMixin, base.Handler):
  @base.route_argument
  @base.require_perm(builtin.PERM_VIEW_HOMEWORK)
  @base.require_perm(builtin.PERM_READ_RECORD_CODE)
  @base.sanitize
  async def get(self, rid: objectid.ObjectId):
    # Report Download Settings
    # rid = objectid.ObjectId("5f5cc9314e26cb78f6bd108b")
    ureports = mdb.ureport.find({"report_id": rid})

    all_uid = set()
    all_year = set()
    all_group = set()
    all_class = set()
    all_uid.add("All")
    all_year.add("All")
    all_group.add("All")
    all_class.add("All")

    for ureport in ureports:
      all_uid.add(ureport['user_id'])
      this_user =  mdb.user.find_one({'_id': ureport['user_id']})
      all_year.add(this_user['year'])
      all_group.add(this_user['group'])
      all_class.add(this_user['_class'])

    all_uid = {r: r for i, r in enumerate(all_uid)}
    all_year = {r: r for i, r in enumerate(all_year)}
    all_group = {r: r for i, r in enumerate(all_group)}
    all_class = {r: r for i, r in enumerate(all_class)}

    download_settings = [
      Setting('setting_download', 'uid', str,
                range=all_uid,
                default="All", ui='select', name='By id'),
      Setting('setting_download', 'year', str,
              range=all_year,
              default="All", ui='select', name='By year'),
      Setting('setting_download', '_class', int, range=all_class,
              ui='select', name='By class', default="All",
              desc='By class'),
      Setting('setting_download', 'group', str, range=all_group,
              ui='select', name='By group',default="All"),
    ]
    report = mdb.report.find_one({'_id': rid})
    self.render('report_download.html', category='report_download', settings=download_settings, tdoc=report)

  @base.route_argument
  @base.post_argument
  @base.require_csrf_token
  @base.require_perm(builtin.PERM_VIEW_HOMEWORK)
  @base.require_perm(builtin.PERM_READ_RECORD_CODE)
  @base.sanitize
  async def post(self, rid: objectid.ObjectId, uid: str, year: str, _class: str, group: str):
    report_filter = {}

    user_filters = {}
    if year != "All":
      user_filters["year"] = year
    if _class != "All":
      user_filters["_class"] = _class
    if group != "All":
      user_filters["group"] = group
    all_users = mdb.user.find(user_filters)
    all_user_ids = [tuser["_id"] for tuser in all_users]

    all_reports = mdb.ureport.find({"report_id": rid, "user_id": {"$in": all_user_ids}})
    print(all_reports)
    output_buffer = io.BytesIO()
    zip_file = zipfile.ZipFile(output_buffer, 'a', zipfile.ZIP_DEFLATED)
    for report in all_reports:
      file_name = report['data']
      with open("data/%s" % file_name, "rb") as f:
        zip_file.writestr(file_name, f.read())

    for zfile in zip_file.filelist:
      zfile.create_system = 0
    zip_file.close()
    await self.binary(output_buffer.getvalue(), 'application/zip',
                      file_name='Archive.zip')
