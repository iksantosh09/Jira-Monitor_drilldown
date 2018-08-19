#!/usr/bin/env python
# Author - Santosh G

import sys, pymysql, pprint, smtplib, math, time, requests, json, base64, argparse, datetime
import threading, Queue
from prettytable import PrettyTable
from email.mime.text import MIMEText
from jira.client import JIRA
from urllib2 import Request, urlopen
from datetime import date, timedelta
from collections import Counter

### Global config parameters ###
DEBUG = 0 # prints extra output

strQueryDateInterval = "7"
strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + strQueryDateInterval + " DAY)"
strZQueryStartDate = "startOfDay(-" + strQueryDateInterval + "d)"

smtpServer = "https://smtp.gmail.com:465"
sender = "iksantosh@gmail.com"
reportRecipients = ["youremailID"]

headers = {"Authorization" : "Basic bmVlcmFqOmRvdGhpZ2gxMjM=", "Content-Type": "application/json"}
#jiraServer = "http://jira.corp.sjc.shn:8080"
jiraServer = "http://jiraserver:8080"
jiraOptions={'server': jiraServer}

MAX_RECORDS = 1000 #forced by JIRA; see https://confluence.atlassian.com/display/JIRAKB/Exporting+a+filter+result+containing+more+than+1000+issues
PROJECT_NAMES = ("Myproject")
TIMEOUT = 300


### Decorator timer function ###
def timeit(f):
	"""Time any function f"""

	def timed(*args, **kw):
		"""Return time to execute function f()"""
		if DEBUG:
			print '%s: Entering %r args:[%r, %r]' % \
			  (datetime.datetime.now().time(), f.__name__, args, kw)

		timeStart = time.time()
		result = f(*args, **kw)
		timeEnd = time.time()

		if DEBUG:
			print '%s: %r args:[%r, %r] took: %2.4f sec' % \
			  (datetime.datetime.now().time(), f.__name__, args, kw, timeEnd-timeStart)
		return result

	return timed


### Decorator threading function ###
def threaded(f, daemon=False):

### Decorator threading function ###
	def wrapped_f(q, *args, **kwargs):
		"""Calls decorated function f and puts the result in a queue"""
		
		try:
			ret = f(*args, **kwargs)
		except Exception, e:
			ret = e
		q.put(ret)


	def wrap(*args, **kwargs):
		"""Function returned from the decorator. Executes function
		wrapped_f in a new thread and returns the thread object with
		the result queue attached"""

		q = Queue.Queue()

		t = threading.Thread(target=wrapped_f, args=(q,)+args, kwargs=kwargs)
		t.daemon = daemon
		t.start()
		t.result_queue = q
		return t

	return wrap


### thread to retrieve requests Zephyr results; returns json ###
@threaded
# @timeit
def getZephyrExecutionResults(expand, numMaxRecords, offset, zqlQuery):
	""" thread to retrieve requests Zephyr results; returns json """
	requestVals = ({
		"expand": expand,
		"maxRecords": numMaxRecords,
		"offset": offset,
		"zqlQuery": zqlQuery
	})
	response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
	return response.json()

### thread to retrieve row of Zephyr trend data ###
@threaded
@timeit
def getZephyrTrendByRow(day, intDayOffset):
	""" thread to retrieve requests Zephyr results; returns row as list with each column """
	
	strZQueryStartOffset = (intDayOffset * day)
	strZQueryEndOffset = (intDayOffset * (day-1))

	strStartDate = str(date.today() - timedelta(days=(strZQueryStartOffset)))
	strEndDate = str(date.today() - timedelta(days=(strZQueryEndOffset)))

	passes = 0
	fails = 0
	WIPs = 0
	numTestsUnique = 0
	uniqueTestsRan = set()
	numTestsExecJIRA = 0
	
	expand = "executionStatus"
	offset = 0 # for JIRA results pagination
	zqlQuery = "executionDate >= startOfDay(-%dd) AND executionDate < endOfDay(-%dd)" % (strZQueryStartOffset, strZQueryEndOffset)

	rqueues = [] # result queues for each jira API call thread

	# determine number of records needing to be fetched		
	rqueues.append(getZephyrExecutionResults(expand, MAX_RECORDS, offset, zqlQuery).result_queue)
	rq = rqueues.pop()
	response = rq.get(TIMEOUT)

	# ensure nothing timed out
	if isinstance(response, Exception):
		raise response

	#interpret results: each response is json emitted from jira
	response_json = response

	numTestsExecJIRA = response_json['totalCount']

	numResultsPages = 1
	if numTestsExecJIRA:
		numResultsPages = -(- numTestsExecJIRA // MAX_RECORDS ) # ceil( numTestsExecJIRA / MAX_RECORDS)

	# fetch Zephyr paginated data; retrieve each page in parallel
	for resultsPage in range(0, numResultsPages):
		offset = MAX_RECORDS * (resultsPage)
		rqueues.append(getZephyrExecutionResults(expand, MAX_RECORDS, offset, zqlQuery).result_queue)

	# combine output data from each Zephyr result page
	for rq in rqueues:
			
		response = rq.get(TIMEOUT)

		# ensure nothing timed out
		if isinstance(response, Exception):
			raise response

		#interpret results: each response is json emitted from jira
		response_json = response

# 		print "Processing %d executions" % len(response_json['executions'])
		
		for i in range(0,len(response_json['executions'])):
			testResult = response_json['executions'][i]
			uniqueTestsRan.add(testResult['issueSummary'])
			status = testResult['status']['name']
			if (status == 'PASS'):
				passes += 1
			if (status == 'FAIL'):
				fails += 1
			if (status == 'WIP'):
				WIPs += 1
	
	numTestsRan = numTestsExecJIRA
	numTestsPassed = passes
	numTestsUnique = len(uniqueTestsRan)

	passRate = 0
	if numTestsRan:
		passRate = (float(numTestsPassed)/float(numTestsRan)) * 100

	# Calculate unique number of tests known at each week's end
	jqlQuery = "issueType = TEST and Automated = YES and createdDate <= endOfDay(-%dd)" % strZQueryEndOffset
	requestVals = ({
		"jql": jqlQuery
	})

	response = requests.request('GET',"%s/rest/api/latest/search" % jiraServer, headers=headers, params=requestVals, verify=False)
	response_json = response.json()

	numTotalTestsUnique = response_json['total']

	strPassRate = "{0:.2f}%".format(passRate) + " (" + str(numTestsPassed) + " / " + str(numTestsRan) + ")"
	strUniqueExec = str(numTestsUnique) + " / " + str(numTotalTestsUnique)

	return (strStartDate, strPassRate, strUniqueExec)

# Above approach for issueType can be performed for User Stories as well
### returns text table of Jira trends for specified dates
@threaded
@timeit
def getJiraTrend(col_names, intStartDay, intEndDay, intDayOffset):
	""" returns text table of Jira trends for specified dates """
	
	rows = []
	rqueues = [] # result queues for each jira API call thread
	
	# fetch each day in parallel
	for day in range(intStartDay, intEndDay, -1):
		rqueues.append(getZephyrTrendByRow(day, intDayOffset).result_queue)
		
	# combine output data from each Zephyr result page
	for rq in rqueues:

		response = rq.get(TIMEOUT)

		# ensure nothing timed out
		if isinstance(response, Exception):
			raise response

		#interpret results: each response is json emitted from jira
		rows.append(response)

	table = PrettyTable(col_names)
	table.align[col_names[1]] = "l" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	return table.get_string()


### Email output ###
def Email(strMessage):
	""" Sends email with message body strMessage """

	global strQueryStartDate, reportRecipients, smtpServer, sender

 	conn = pymysql.connect(user='username', password='password1',
								  host='ipaddress',
 								  database='databasetostore')
 
 	cursor = conn.cursor()

	### Config parameters ###
	addrSeperator = ", "
	
	strQuery = 'SELECT ' + strQueryStartDate
	cursor.execute(strQuery)
	strSQLStartDate = str(cursor.fetchone()[0])

	strQuery = 'SELECT CURRENT_DATE()'
	cursor.execute(strQuery)
	strSQLCurrDate = str(cursor.fetchone()[0])

	msg = MIMEText(strMessage)
	msg['Subject'] = "SHN QA Report for " + strSQLStartDate + " through " + strSQLCurrDate + " (UTC)"
	msg['From'] = sender
	msg['To'] = addrSeperator.join(reportRecipients)

 	conn.close()

	s = smtplib.SMTP(smtpServer)
	s.sendmail(sender, reportRecipients, msg.as_string())
	s.quit()	


### Intro ###
def Intro():

	global strQueryDateInterval
	
	strStartDate = str(date.today() - timedelta(days=(int(strQueryDateInterval))))
	strEndDate = str(date.today())

	print(chr(27) + "[2J")
	
	print
	print "-----------------------------------------------"
	print "Project Drilldown Report for " + strStartDate + " through " + strEndDate
	print "-----------------------------------------------"
	print


### Top 10 Tests Run ###
@threaded
@timeit
def TopTen():

	strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + strQueryDateInterval + " DAY)"
	
	strMessage = """\


Most-executed Tests, Features, Userstories, Tasks in AutoDB (last 7 days)    
-------------------------------------------

"""

	strQuery = 'SELECT COUNT(*), executionsummary.`TestName` FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' GROUP BY executionsummary.`TestName` ORDER BY COUNT(*) DESC LIMIT 10'

	conn = pymysql.connect(user='username', password='password1',
								  host='ipaddress',
								  database='databasestore')

	cursor = conn.cursor()
	
	cursor.execute(strQuery)

	col_names = [col[0] for col in cursor.description]
	rows = cursor.fetchall()
	
	conn.close()

	table = PrettyTable(col_names)
	table.align[col_names[1]] = "l"
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()
	
	return strMessage


### Top 10 Tests Run in Jira ###
@threaded
@timeit
def TopTenJira():

	strMessage = """\


Most-executed Tests, Features, Userstories, Tasks in AutoDB (last 7 days)  
-----------------------------------------

"""

	pp = pprint.PrettyPrinter(indent=4)
	
	col_names = ["Count", "Test Summary"]
	rows = []

	### fetch Zephyr data		
	expand = "executionStatus"
	offset = 0 # for JIRA results pagination
	zqlQuery = "executionDate >= startOfDay(-7d)"

	requestVals = ({
		"expand": expand,
		"maxRecords": MAX_RECORDS,
		"offset": offset,
		"zqlQuery": zqlQuery
	})
	response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
	response_json = response.json()

	numTestsExecJIRA = response_json['totalCount']

	passes = 0
	fails = 0
	WIPs = 0
	listOfTestsRun = []

	numResultsPages = 1
	if numTestsExecJIRA:
		numResultsPages = -(- numTestsExecJIRA // MAX_RECORDS ) # ceil( numTestsExecJIRA / MAX_RECORDS)

	for resultsPage in range(0, numResultsPages):

		offset = MAX_RECORDS * (resultsPage+1)
	
		for i in range(0,len(response_json['executions'])):
			testResult = response_json['executions'][i]
			listOfTestsRun.append(testResult['issueSummary'])
			status = testResult['status']['name']
			if (status == 'PASS'):
				passes += 1
			if (status == 'FAIL'):
				fails += 1
			if (status == 'WIP'):
				WIPs += 1

		requestVals = ({
			"expand": expand,
			"maxRecords": MAX_RECORDS,
			"offset": offset,
			"zqlQuery": zqlQuery
		})
		response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
		response_json = response.json()
	
	topTests = Counter(listOfTestsRun).most_common(10)
	
	for item in topTests:
		rows.append(item[::-1])

	table = PrettyTable(col_names)
	table.align[col_names[1]] = "l" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()
	
	return strMessage


### Trends ###
@threaded
@timeit
def Trends():

	strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + strQueryDateInterval + " DAY)"

	strMessage = """\


Trending in AutoDB
------------------

"""

	### 7 day trend

	col_names = ["Day", "Pass Rate", "# unique executed"]
	rows = []
	
	conn = pymysql.connect(user='AutoDBUser', password='portal123!',
								  host='172.16.204.44',
								  database='AutomationDB')

	cursor = conn.cursor()

	for x in range(7,0,-1):

		strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + str(x) + " DAY)"
		strQueryEndDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + str(x-1) + " DAY)"

		strQuery = 'SELECT ' + strQueryStartDate
		cursor.execute(strQuery)
		strSQLStartDate = str(cursor.fetchone()[0])

		strQuery = 'SELECT ' + strQueryEndDate
		cursor.execute(strQuery)
		strSQLEndDate = str(cursor.fetchone()[0])

		strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' AND executionsummary.`ExecutionDate` < ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTestsRan = cursor.fetchone()[0]

		strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`Status` = "PASS" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' AND executionsummary.`ExecutionDate` < ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTestsPassed = cursor.fetchone()[0]

		strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' AND executionsummary.`ExecutionDate` < ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTestsUnique = cursor.fetchone()[0]

		passRate = (float(numTestsPassed)/float(numTestsRan)) * 100

		strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`ExecutionDate` <= ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTotalTestsUnique = cursor.fetchone()[0]

		strWeek = strSQLEndDate
		strPassRate = "{0:.2f}%".format(passRate) + " (" + str(numTestsPassed) + " / " + str(numTestsRan) + ")"
		strUniqueExec = str(numTestsUnique) + " / " + str(numTotalTestsUnique)

		rows.append((strWeek, strPassRate, strUniqueExec))

		# end for

	table = PrettyTable(col_names)
	table.align[col_names[1]] = "l" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()

	strMessage += "\n"

	### 10 week trend

	col_names = ["Week of...", "Pass Rate", "# unique executed"]
	rows = []

	for x in range(10,0,-1):

		strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + str(7 * x) + " DAY)"
		strQueryEndDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + str(7 * (x-1)) + " DAY)"

		strQuery = 'SELECT ' + strQueryStartDate
		cursor.execute(strQuery)
		strSQLStartDate = str(cursor.fetchone()[0])

		strQuery = 'SELECT ' + strQueryEndDate
		cursor.execute(strQuery)
		strSQLEndDate = str(cursor.fetchone()[0])

		strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' AND executionsummary.`ExecutionDate` < ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTestsRan = cursor.fetchone()[0]

		strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`Status` = "PASS" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' AND executionsummary.`ExecutionDate` < ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTestsPassed = cursor.fetchone()[0]

		strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ' AND executionsummary.`ExecutionDate` < ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTestsUnique = cursor.fetchone()[0]

		passRate = (float(numTestsPassed)/float(numTestsRan)) * 100

		strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`ExecutionDate` <= ' + strQueryEndDate + ''
		cursor.execute(strQuery)
		numTotalTestsUnique = cursor.fetchone()[0]

		strWeek = strSQLEndDate
		strPassRate = "{0:.2f}%".format(passRate) + " (" + str(numTestsPassed) + " / " + str(numTestsRan) + ")"
		strUniqueExec = str(numTestsUnique) + " / " + str(numTotalTestsUnique)

		rows.append((strWeek, strPassRate, strUniqueExec))

		# end for

	conn.close()

	table = PrettyTable(col_names)
	table.align[col_names[1]] = "l" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()
	
	return strMessage


### Trends ###
@threaded
@timeit
def TrendsJira():

	strMessage = """\


Trending in Jira
----------------

"""

	rqueues = []
	
	### 7 day trend

	col_names = ["Day", "Pass Rate", "# unique executed"]
	rows = []

	rqueues.append(getJiraTrend(col_names, 7, 0, 1).result_queue)

	### 10 week trend

	col_names = ["Week of...", "Pass Rate", "# unique executed"]
	rows = []
	
	rqueues.append(getJiraTrend(col_names, 10, 0, 7).result_queue)

	# get results
	for rq in rqueues:
		response = rq.get(TIMEOUT)
		if isinstance(response, Exception):
			raise response
		strMessage += response + "\n"

	return strMessage


### Suite Stats ###
@threaded
@timeit
def Suites():

	strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + strQueryDateInterval + " DAY)"
	
	conn = pymysql.connect(user='AutoDBUser', password='portal123!',
								  host='172.16.204.44',
								  database='AutomationDB')

	cursor = conn.cursor()

	strQuery = 'SELECT ' + strQueryStartDate
	cursor.execute(strQuery)
	strSQLStartDate = str(cursor.fetchone()[0])

	strQuery = 'SELECT CURRENT_DATE()'
	cursor.execute(strQuery)
	strSQLCurrDate = str(cursor.fetchone()[0])

	numSuitesRan = 0

	strQuery = 'SELECT COUNT(DISTINCT executionsummary.`Suite`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
	cursor.execute(strQuery)
	numSuitesRan = cursor.fetchone()[0]

	strQuery = 'SELECT DISTINCT executionsummary.`Suite` FROM executionsummary WHERE executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
	cursor.execute(strQuery)
	suitesRan = cursor.fetchall()

	strMessage = """\


Suite Stats in AutoDB for %s through %s    
---------------------------------------------------------

""" % (strSQLStartDate, strSQLCurrDate)

	col_names = ["Suite", "Pass Rate", "# unique executed"]
	rows = []

 	for suiteName in suitesRan:
		strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`Suite` = "' + suiteName[0] + '" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
		cursor.execute(strQuery)
		numTestsInSuiteRan = cursor.fetchone()[0]
		
		suitePassRate = 0
		suitePassRateAdj = 0
		
		if numTestsInSuiteRan:

			strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`Status` = "PASS" AND executionsummary.`Suite` = "' + suiteName[0] + '" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
			cursor.execute(strQuery)
			numTestsInSuitePassed = cursor.fetchone()[0]

			strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`Status` = "FAIL" AND executionsummary.`Suite` = "' + suiteName[0] + '" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
			cursor.execute(strQuery)
			numTestsInSuiteFailed = cursor.fetchone()[0]

			strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`Suite` = "' + suiteName[0] + '" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
			cursor.execute(strQuery)
			numTestsInSuiteUnique = cursor.fetchone()[0]

			suitePassRate = (float(numTestsInSuitePassed)/float(numTestsInSuiteRan)) * 100
			suitePassRateAdj = (1-(float(numTestsInSuiteFailed)/float(numTestsInSuiteRan))) * 100

			if suiteName[0] != "":
				strSuite = suiteName[0]
				strSuitePassRate = "{0:.2f}%".format(suitePassRate) + " (" + str(numTestsInSuitePassed) + " / " + str(numTestsInSuiteRan) + "), " + "{0:.2f}%".format(suitePassRateAdj) + " adjusted"
				strSuiteUniqueExec = str(numTestsInSuiteUnique)

			rows.append((strSuite, strSuitePassRate, strSuiteUniqueExec))
	
	conn.close()

	table = PrettyTable(col_names)
	table.align[col_names[0]] = "l" 
	table.align[col_names[1]] = "r" 
	table.align[col_names[2]] = "r" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()
	
	return strMessage


### Suite Stats ###
@threaded
@timeit
def Products():
	
	conn = pymysql.connect(user='AutoDBUser', password='portal123!',
								  host='172.16.204.44',
								  database='AutomationDB')

	cursor = conn.cursor()

	strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + strQueryDateInterval + " DAY)"
	
	strQuery = 'SELECT ' + strQueryStartDate
	cursor.execute(strQuery)
	strSQLStartDate = str(cursor.fetchone()[0])

	strQuery = 'SELECT CURRENT_DATE()'
	cursor.execute(strQuery)
	strSQLCurrDate = str(cursor.fetchone()[0])

	numTestsInProjectRan = 0
	suitesRan = []

	for project in PROJECT_NAMES:

		strQuery = 'SELECT COUNT(*) FROM executionsummary WHERE executionsummary.`TestID` LIKE "' + project + '%" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
		cursor.execute(strQuery)
		numTestsInProjectRan = cursor.fetchone()[0]
		if numTestsInProjectRan:
		    suitesRan.append(project)

	strMessage = """\


Product Stats in AutoDB for %s through %s    
---------------------------------------------------------

""" % (strSQLStartDate, strSQLCurrDate)

	col_names = ["Suite", "Pass Rate", "# unique executed"]
	rows = []
	
	for project in suitesRan:

		if DEBUG:
			print "Suite name = '" + project + "'"

		strQuery = 'SELECT COUNT(*) FROM executionsummary WHERE executionsummary.`TestID` LIKE "' + project + '%" AND executionsummary.`Status` = "PASS" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
		cursor.execute(strQuery)
		numTestsInSuitePassed = cursor.fetchone()[0]
	
		strQuery = 'SELECT COUNT(*) FROM executionsummary WHERE executionsummary.`TestID` LIKE "' + project + '%" AND executionsummary.`Status` = "FAIL" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
		cursor.execute(strQuery)
		numTestsInSuiteFailed = cursor.fetchone()[0]
	
		strQuery = 'SELECT COUNT(*) FROM executionsummary WHERE executionsummary.`TestID` LIKE "' + project + '%" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
		cursor.execute(strQuery)
		numTestsInSuiteRan = cursor.fetchone()[0]
		
		if DEBUG:
			print "Suite tests ran = '" + str(numTestsInSuiteRan) + "'"
			print "Query = " + strQuery
			print "strQueryDateInterval = " + strQueryDateInterval
		
		strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`TestID` LIKE "' + project + '%" AND executionsummary.`ExecutionDate` >= ' + strQueryStartDate + ''
		cursor.execute(strQuery)
		numTestsInSuiteUnique = cursor.fetchone()[0]

		suitePassRate = 0
		suitePassRateAdj = 0
		
		if numTestsInSuiteRan:
			suitePassRate = (float(numTestsInSuitePassed)/float(numTestsInSuiteRan)) * 100
			suitePassRateAdj = (1-(float(numTestsInSuiteFailed)/float(numTestsInSuiteRan))) * 100
	
		strSuite = project
		strSuitePassRate = "{0:.2f}%".format(suitePassRate) + " (" + str(numTestsInSuitePassed) + " / " + str(numTestsInSuiteRan) + "), " + "{0:.2f}%".format(suitePassRateAdj) + " adjusted"
		strSuiteUniqueExec = str(numTestsInSuiteUnique)

		rows.append((strSuite, strSuitePassRate, strSuiteUniqueExec))
	
	conn.close()

	table = PrettyTable(col_names)
	table.align[col_names[0]] = "l" 
	table.align[col_names[1]] = "r" 
	table.align[col_names[2]] = "r" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()
	
	return strMessage


### JIRA Suite Stats ###
@threaded
@timeit
def ProductsJira():

	col_names = ["Suite", "Pass Rate", "# unique executed"]
	rows = []
	
	strSQLStartDate = str(date.today() - timedelta(days=(7)))
	strSQLEndDate = str(date.today() - timedelta(days=0))

	strMessage = """\


Product Stats in Jira for %s through %s    
-------------------------------------------------------

""" % (strSQLStartDate, strSQLEndDate)

	for project in PROJECT_NAMES:
	
		### fetch Zephyr data		
		expand = "executionStatus"
		offset = 0 # for JIRA results pagination
		zqlQuery = "project = %s AND executionDate >= startOfDay(-7d)" % project

		requestVals = ({
			"expand": expand,
			"maxRecords": MAX_RECORDS,
			"offset": offset,
			"zqlQuery": zqlQuery
		})
		response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
		response_json = response.json()
		
		if "totalCount" in response_json:
			numTestsExecJIRA = response_json['totalCount']

			passes = 0
			fails = 0
			WIPs = 0
			numTestsUnique = 0
			listOfTestsRun = []
			uniqueTestsRan = set()

			numResultsPages = 1
			if numTestsExecJIRA:
				numResultsPages = -(- numTestsExecJIRA // MAX_RECORDS ) # ceil( numTestsExecJIRA / MAX_RECORDS)

			for resultsPage in range(0, numResultsPages):

				offset = MAX_RECORDS * (resultsPage+1)
	
				for i in range(0,len(response_json['executions'])):
					testResult = response_json['executions'][i]
					listOfTestsRun.append(testResult['issueSummary'])
					uniqueTestsRan.add(testResult['issueSummary'])
					status = testResult['status']['name']
					if (status == 'PASS'):
						passes += 1
					if (status == 'FAIL'):
						fails += 1
					if (status == 'WIP'):
						WIPs += 1

				requestVals = ({
					"expand": expand,
					"maxRecords": MAX_RECORDS,
					"offset": offset,
					"zqlQuery": zqlQuery
				})
				response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
				response_json = response.json()
	
			numTestsInSuitePassed = passes
			numTestsInSuiteRan = len(listOfTestsRun)
			numTestsInSuiteUnique = len(uniqueTestsRan)
	
			suitePassRate = 0
			if numTestsInSuiteRan:
				suitePassRate = (float(numTestsInSuitePassed)/float(numTestsInSuiteRan)) * 100
			
			strSuite = project
			strSuitePassRate = "{0:.2f}%".format(suitePassRate) + " (" + str(numTestsInSuitePassed) + " / " + str(numTestsInSuiteRan) + ")"
			strSuiteUniqueExec = str(numTestsInSuiteUnique)

			rows.append((strSuite, strSuitePassRate, strSuiteUniqueExec))

	table = PrettyTable(col_names)
	table.align[col_names[0]] = "l" 
	table.align[col_names[1]] = "r" 
	table.align[col_names[2]] = "r" 
	table.padding_width = 1    
	for row in rows:
		table.add_row(row)

	strMessage += table.get_string()
	
	return strMessage
	
### Combined Stats for Today ###
@threaded
@timeit
def Todays():

	strQueryStartDate = "SUBDATE(CURRENT_DATE(), INTERVAL " + strQueryDateInterval + " DAY)"

	strMessage = """\


Today's AutoDB Stats    
--------------------

"""
	
	conn = pymysql.connect(user='AutoDBUser', password='portal123!',
								  host='172.16.204.44',
								  database='AutomationDB')

	cursor = conn.cursor()

	strQuery = 'SELECT ' + strQueryStartDate
	cursor.execute(strQuery)
	strSQLStartDate = str(cursor.fetchone()[0])

	strQuery = 'SELECT CURRENT_DATE()'
	cursor.execute(strQuery)
	strSQLCurrDate = str(cursor.fetchone()[0])

	strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= CURRENT_DATE()'
	cursor.execute(strQuery)
	numTestsRan = cursor.fetchone()[0]

	strMessage += "Total number of tests executed today: %d \n" % numTestsRan

	strQuery = 'SELECT COUNT(executionsummary.`Status`) FROM executionsummary WHERE executionsummary.`Status` = "PASS" AND executionsummary.`ExecutionDate` >= CURRENT_DATE()'
	cursor.execute(strQuery)
	numTestsPassed = cursor.fetchone()[0]

	strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary WHERE executionsummary.`ExecutionDate` >= CURRENT_DATE()'
	cursor.execute(strQuery)
	numTestsUnique = cursor.fetchone()[0]

	passRate = 0
	if numTestsRan:
		passRate = (float(numTestsPassed)/float(numTestsRan)) * 100

	strMessage += ("Today's pass rate: {0:.2f}%".format(passRate) + " (%d passed, %d unique) \n" % (numTestsPassed, numTestsUnique))

	strQuery = 'SELECT COUNT(DISTINCT executionsummary.`TestName`) FROM executionsummary'
	cursor.execute(strQuery)
	numTotalTestsUnique = cursor.fetchone()[0]
	
	conn.close()

	strMessage += str(numTestsUnique) + " out of " +  str(numTotalTestsUnique) + " known automated test cases executed today"
	
	return strMessage
	
	
### Combined JIRA Stats for Today ###
### XXX TODO: Separate output of automated vs. manual tests
@threaded
@timeit
def TodaysJira():

	strMessage = """\


Today's Jira Stats    
------------------

"""
	pp = pprint.PrettyPrinter(indent=4)
	
	expand = "executionStatus"
	offset = 0 # for JIRA results pagination
	zqlQuery = "executionStatus != 'UNEXECUTED' AND executionDate >= startOfDay()"
	
	requestVals = ({
		"expand": expand,
		"maxRecords": MAX_RECORDS,
		"offset": offset,
		"zqlQuery": zqlQuery
	})
	response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
	response_json = response.json()

	numTestsExecJIRA = response_json['totalCount']
	strMessage += "Total number of tests executed today: %d \n" % numTestsExecJIRA 

	passes = 0
	fails = 0
	WIPs = 0
	numTestsUnique = 0
	uniqueTestsRan = set()

	numResultsPages = 1
	if numTestsExecJIRA:
		numResultsPages = -(- numTestsExecJIRA // MAX_RECORDS ) # ceil( numTestsExecJIRA / MAX_RECORDS)

	for resultsPage in range(0, numResultsPages):
	
		offset = MAX_RECORDS * (resultsPage+1)
		
		for i in range(0,len(response_json['executions'])):
			testResult = response_json['executions'][i]
			uniqueTestsRan.add(testResult['issueSummary'])
			status = testResult['status']['name']
			if (status == 'PASS'):
				passes += 1
			if (status == 'FAIL'):
				fails += 1
			if (status == 'WIP'):
				WIPs += 1

		requestVals = ({
			"expand": expand,
			"maxRecords": MAX_RECORDS,
			"offset": offset,
			"zqlQuery": zqlQuery
		})
		response = requests.request('GET',"%s/rest/zapi/latest/zql/executeSearch" % jiraServer, headers=headers, params=requestVals, verify=False)
		response_json = response.json()

	numTestsRan = numTestsExecJIRA
	numTestsPassed = passes
	numTestsUnique = len(uniqueTestsRan)
	
	passRate = 0
	if numTestsRan:
		passRate = (float(numTestsPassed)/float(numTestsRan)) * 100

	strMessage += ("Today's pass rate: {0:.2f}%".format(passRate) + " (%d passed, %d unique) \n" % (numTestsPassed, numTestsUnique))
	
	
	jqlQuery = "issueType = TEST and Automated = YES"
	requestVals = ({
		"jql": jqlQuery
	})
	
	response = requests.request('GET',"%s/rest/api/latest/search" % jiraServer, headers=headers, params=requestVals, verify=False)
	response_json = response.json()
	
	numTotalTestsUnique = response_json['total']

 	strMessage += str(numTestsUnique) + " out of " +  str(numTotalTestsUnique) + " known automated test cases executed today"
	
	return strMessage


### main ###
def main():

	global DEBUG
	sendEmail = 0

	parser = argparse.ArgumentParser(description='Creates daily test automation status report.')
	parser.add_argument('-t', '--sourceType', type=str, required=True,
						help='data source to use (one of: autodb, jira, or all)')
	parser.add_argument('-d', '--debug', action='store_true', default=False,
						help='Disables email and adds extra console output if specified')
	parser.add_argument('-e', '--email', action='store_true', default=False,
						help='Sends email if specified')

	args = parser.parse_args()

	sourceType = args.sourceType # options: autodb, jira, all
	DEBUG = args.debug
	sendEmail = args.email

	if DEBUG:
		print "Using " + sourceType + " for results"

	msgText = ""

	Intro()

	if sourceType == "autodb":
		rqueues = [func().result_queue for func in (Todays, Trends, TopTen, Suites, Products)]
	elif sourceType == "jira":
		rqueues = [func().result_queue for func in (TodaysJira, TrendsJira, TopTenJira, ProductsJira)]
	elif sourceType == "all":
		rqueues = [func().result_queue for func in (Todays, TodaysJira, Trends, TrendsJira, TopTen, TopTenJira, Suites, Products, ProductsJira)]
	else:
		sys.exit("Usage: qareport.py [sourceType] (sourceType = [autodb, jira, all])")

	for rq in rqueues:
		response = rq.get(TIMEOUT)
		if isinstance(response, Exception):
			raise response
		msgText += response

	print msgText
	print

	if not DEBUG:
			if sendEmail:
					Email(msgText)
					

if __name__=="__main__":
    main()
