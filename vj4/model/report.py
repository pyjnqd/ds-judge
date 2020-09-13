import itertools
from bson import objectid
from pymongo import ReturnDocument
import datetime

from vj4.util import argmethod
from vj4.db import mdb
from vj4.model import builtin


def report_rename(self, student, report, file_extension):
  course_id = report['course']
  # 数据结构
  if course_id == 0:
    return "%s.%s-%s-%s-实验%s.%s" % (
      str(student['year'])[-2:],
      student['_class'],
      student['_id'][1:],
      student['name'],
      builtin.NUMBER_TRANSLATE[report['report_id']],
      file_extension
    )

  return None




